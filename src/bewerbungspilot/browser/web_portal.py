"""Real web forms through Playwright (phase 5.1) — generic observer plus a Personio layer.

Rules that no caller can bypass:

* ``submit`` exists, but only ``browser.runner.submit_application`` calls it, after the
  user's single-use authorization; ``dry_run=True`` (the default) refuses to click at all;
* cookie banners: only the most privacy-preserving choice is ever clicked;
* CAPTCHA, login or one-time codes are never handled — the page is left to the user;
* nothing outside the form is clicked except the portal's own "apply" entry button.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from .portal import FieldKind, FormField, FormSnapshot, PortalError, SubmissionOutcome

OBSERVE_JS = r"""
() => {
  const visible = el => { const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none' && (r.width > 0 || r.height > 0); };
  // Personio & co. print "E-Mail*\n(erforderlich)" / "Anschreiben (Optional)": the markers decide
  // "required", then are stripped from the label.
  const MARK = /\*|\((?:erforderlich|required|pflichtfeld|obligatoire|optional|facultatif)\)/gi;
  const clean = t => (t || '').replace(MARK, ' ').replace(/\s+/g, ' ').trim();
  const isRequiredText = t => /\*|\((?:erforderlich|required|pflichtfeld|obligatoire)\)/i.test(t || '') && !/\(optional|facultatif\)/i.test(t || '');
  const BUTTON_WORDS = /datei hinzufügen|add files?|upload|hochladen|durchsuchen|browse|choose files?|datei auswählen/gi;
  const rawLabelOf = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l && clean(l.innerText)) return l.innerText; }
    if (el.getAttribute('aria-labelledby')) { const t = el.getAttribute('aria-labelledby').split(' ').map(i => document.getElementById(i)).filter(Boolean).map(n => n.innerText).join(' '); if (clean(t)) return t; }
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const wrap = el.closest('label'); if (wrap && clean(wrap.innerText)) return wrap.innerText;
    const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg && clean(lg.innerText)) return lg.innerText; }
    if ((el.type || '').toLowerCase() === 'file') {
      // Hidden file input behind an "add file" button: the nearest ancestor text is its title.
      let a = el.parentElement;
      for (let i = 0; a && i < 4; i++, a = a.parentElement) {
        const line = (a.innerText || '').replace(BUTTON_WORDS, '').split('\n').map(x => x.trim()).filter(Boolean)[0];
        if (line) return line;
      }
    }
    if (el.placeholder) return el.placeholder;
    return el.name || el.id || '';
  };
  const labelOf = el => clean(rawLabelOf(el));
  const requiredOf = el => el.required || el.getAttribute('aria-required') === 'true' || isRequiredText(rawLabelOf(el));
  const out = []; const radios = {};
  let n = 0;
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = (el.type || el.tagName).toLowerCase();
    // File inputs are usually hidden behind a button: keep them, they are filled with set_input_files.
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type) || el.disabled || (type !== 'file' && !visible(el))) continue;
    // Stable ids across reloads: element id, then name, then position.
    const base = el.id ? `id-${el.id}` : (el.name && type !== 'radio') ? `name-${el.name}` : `pos-${n}`;
    n++;
    const key = type === 'radio' ? `radio-${el.name || base}` : base;
    el.setAttribute('data-bp', key);
    if (type === 'radio') {
      const group = el.name || base;
      const fs = el.closest('fieldset'); const legend = fs && fs.querySelector('legend');
      const optionLabel = labelOf(el);
      if (!radios[group]) { radios[group] = {key, group, label: clean(legend ? legend.innerText : group), kind: 'radio', required: requiredOf(el), options: [], value: null}; out.push(radios[group]); }
      radios[group].options.push(optionLabel);
      if (el.checked) radios[group].value = optionLabel;
      continue;
    }
    let kind = {email: 'email', tel: 'phone', number: 'number', date: 'date', file: 'file', checkbox: 'checkbox', textarea: 'textarea'}[type]
      || (el.tagName === 'SELECT' ? 'select' : 'text');
    const options = el.tagName === 'SELECT' ? [...el.options].filter(o => o.value !== '').map(o => clean(o.text)) : [];
    let value = el.tagName === 'SELECT' ? (el.selectedIndex > 0 ? clean(el.options[el.selectedIndex].text) : null)
      : type === 'checkbox' ? (el.checked ? 'checked' : null)
      : type === 'file' ? ([...el.files].map(f => f.name).join(', ') || null) : (el.value || null);
    out.push({key, label: labelOf(el), kind, required: requiredOf(el), options, value, html_type: type});
  }
  return out;
}
"""

UPLOADING = re.compile(r"werden hochgeladen|wird hochgeladen|uploading|upload in progress", re.I)

READ_JS = r"""
() => {
  const out = {};
  for (const el of document.querySelectorAll('[data-bp]')) {
    const key = el.getAttribute('data-bp'); const type = (el.type || '').toLowerCase();
    if (type === 'radio') { if (el.checked) { const l = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        out[`radio:${el.name}`] = ((l && l.innerText) || el.closest('label')?.innerText || el.value || '').replace(/\s+/g,' ').trim(); } continue; }
    if (el.tagName === 'SELECT') out[key] = el.selectedIndex > 0 ? el.options[el.selectedIndex].text.replace(/\s+/g,' ').trim() : null;
    else if (type === 'checkbox') out[key] = el.checked ? 'checked' : null;
    else if (type === 'file') out[key] = [...el.files].map(f => f.name).join(', ') || null;
    else out[key] = el.value || null;
  }
  return out;
}
"""

COOKIE_REJECT = re.compile(r"^(nur notwendige|nur erforderliche|ablehnen|alle ablehnen|reject all|reject|decline|only necessary|necessary only|refuser)", re.I)
BLOCKERS = re.compile(r"captcha|recaptcha|hcaptcha|verify you are human|ich bin kein roboter|einmalcode|one-time code|verification code", re.I)
SUCCESS = re.compile(r"thank you|vielen dank|we have received|wir haben ihre bewerbung|erfolgreich (übermittelt|eingegangen|versendet)|application (has been )?(submitted|received)", re.I)


class WebPortal:
    """Playwright ``Page`` wrapped behind the ``PortalAdapter`` contract."""

    def __init__(self, page: Any, *, name: str, submit_button: re.Pattern[str] | None = None, dry_run: bool = True) -> None:
        self.page = page
        self.name = name
        self.dry_run = dry_run
        self.submit_button = submit_button or re.compile(r"^(submit application|bewerbung (absenden|senden|abschicken)|absenden|submit|send application|jetzt bewerben)$", re.I)
        self._fields: dict[str, dict[str, Any]] = {}
        self._uploaded: dict[str, str] = {}

    # ------------------------------------------------------------------ helpers
    def dismiss_cookies(self) -> str | None:
        for button in self.page.get_by_role("button").all():
            try:
                text = (button.inner_text(timeout=500) or "").strip()
            except Exception:  # noqa: BLE001 - detached nodes
                continue
            if COOKIE_REJECT.match(text):
                button.click()
                return text
        return None

    def blocker(self) -> str | None:
        content = self.page.content()
        found = BLOCKERS.search(content)
        return found[0] if found else None

    # ------------------------------------------------------------------ contract
    def observe(self) -> FormSnapshot:
        if blocker := self.blocker():
            raise PortalError(f"Intervention humaine nécessaire sur la page : {blocker}")
        raw = self.page.evaluate(OBSERVE_JS)
        self._fields = {f["key"]: f for f in raw}
        fields = [FormField(id=f["key"], label=f["label"] or f["key"], kind=FieldKind(f["kind"]), required=bool(f["required"]),
                            options=f.get("options", []), value=f.get("value")) for f in raw]
        return FormSnapshot(portal=self.name, title=self.page.title(), fields=fields)

    def _locator(self, field_id: str):
        return self.page.locator(f'[data-bp="{field_id}"]')

    def fill(self, field_id: str, value: str) -> None:
        field = self._fields.get(field_id)
        if field is None:
            raise PortalError(f"Champ inconnu : {field_id}")
        kind = field["kind"]
        if kind == "select":
            self._locator(field_id).select_option(label=value)
        elif kind == "radio":
            group = field["group"]
            radios = self.page.locator(f'input[type="radio"][name="{group}"]')
            for index in range(radios.count()):
                radio = radios.nth(index)
                label = self.page.evaluate(
                    "el => { const l = el.id && document.querySelector(`label[for=\"${CSS.escape(el.id)}\"]`); return ((l && l.innerText) || el.closest('label')?.innerText || el.value || '').replace(/\\s+/g,' ').trim(); }",
                    radio.element_handle())
                if label == value:
                    radio.check()
                    return
            raise PortalError(f"Option introuvable : {value}")
        elif kind == "checkbox":
            self._locator(field_id).set_checked(bool(value))
        elif kind == "date" and field.get("html_type") == "date":
            self._locator(field_id).fill(_to_iso(value))
        else:
            self._locator(field_id).fill(value)

    def upload(self, field_id: str, path: Path) -> None:
        if self._fields.get(field_id, {}).get("kind") != "file" or not Path(path).is_file():
            raise PortalError(f"Téléversement impossible : {path}")
        self._locator(field_id).set_input_files(str(path))
        self._uploaded[field_id] = Path(path).name
        self._wait_uploads()

    def _wait_uploads(self, timeout_ms: int = 30000) -> None:
        """Personio uploads each file at once ("Dateien werden hochgeladen …"): wait until done."""
        busy = self.page.get_by_text(UPLOADING)
        waited = 0
        while busy.count() and waited < timeout_ms:
            self.page.wait_for_timeout(250)
            waited += 250
        if busy.count():
            raise PortalError("Téléversement toujours en cours après 30 s")

    def read_back(self) -> dict[str, str | None]:
        values = self.page.evaluate(READ_JS)
        result: dict[str, str | None] = {}
        for key, field in self._fields.items():
            if field["kind"] == "radio":
                result[key] = values.get(f"radio:{field['group']}")
            else:
                value = values.get(key)
                if field["kind"] == "file" and not value and key in self._uploaded:
                    # Some portals move the file into their own list and clear the input: the
                    # file name shown on the page is the read-back.
                    name = self._uploaded[key]
                    value = name if self.page.get_by_text(name, exact=False).count() else None
                if field["kind"] == "date" and field.get("html_type") == "date" and value:
                    value = _from_iso(value)
                result[key] = value
        return result

    def submit(self) -> SubmissionOutcome:
        if self.dry_run:
            raise PortalError("Mode essai : l'envoi est désactivé")
        buttons = [b for b in self.page.get_by_role("button").all() if self.submit_button.match((b.inner_text() or "").strip())]
        if len(buttons) != 1:
            raise PortalError(f"Bouton d'envoi introuvable ou ambigu ({len(buttons)})")
        buttons[0].click()
        try:
            self.page.wait_for_function(
                "re => new RegExp(re, 'i').test(document.body.innerText)", arg=SUCCESS.pattern, timeout=30_000)
        except Exception as exc:  # noqa: BLE001 - timeout or navigation: outcome unknown after the click
            raise PortalError(f"Aucune confirmation visible après le clic : {exc}") from exc
        text = self.page.inner_text("body")
        match = SUCCESS.search(text)
        snippet = text[max(0, match.start() - 60): match.end() + 120].strip() if match else text[:180]
        return SubmissionOutcome(confirmed=True, reference=self.page.url, message=re.sub(r"\s+", " ", snippet))

    def check_status(self) -> SubmissionOutcome:
        text = self.page.inner_text("body")
        if SUCCESS.search(text):
            return SubmissionOutcome(confirmed=True, reference=self.page.url, message="Confirmation visible sur la page")
        return SubmissionOutcome(confirmed=False, message="Aucune confirmation visible : vérifier la boîte mail")

    def screenshot(self, path: Path) -> Path:
        self.page.screenshot(path=str(path), full_page=True)
        return path


def personio_apply_url(url: str) -> str:
    """``https://x.jobs.personio.de/job/123?language=de`` → ``https://x.jobs.personio.de/job/123/apply?...``"""

    match = re.match(r"^(https://[a-z0-9-]+\.jobs\.personio\.(?:de|com)/job/\d+)(/apply)?(\?.*)?$", url.strip(), re.I)
    if not match:
        raise ValueError("Adresse Personio attendue : https://<entreprise>.jobs.personio.de/job/<numéro>")
    return f"{match[1]}/apply{match[3] or ''}"


def _to_iso(value: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    day, month, year = (int(p) for p in value.split("."))
    return date(year, month, day).isoformat()


def _from_iso(value: str) -> str:
    year, month, day = (int(p) for p in value.split("-"))
    return f"{day:02d}.{month:02d}.{year}"
