# FICTIONAL generator used by the tests. It must be parsed, never executed.
from pathlib import Path

Path("THIS_FILE_MUST_NEVER_BE_CREATED.txt").write_text("executed!")  # proof that parsing does not run code

INK, MUTED, ACCENT = "#18232F", "#4D5966", "#B00020"


def link(label, url):
    return f'<link href="{url}" color="{ACCENT}">{label}</link>'


def make_cv():
    para("Alex Beispiel", 27, 31, "#000000", True, 3)
    para("INFORMATIKSTUDENT | JAVA · WEB", 10.1, 14, ACCENT, True, 5)
    para("Musterstadt · " + link("alex.beispiel@example.org", "mailto:alex.beispiel@example.org"), 9.3, 12.8, MUTED, after=1)
    para(link("github.com/example-user", "https://github.com/example-user"), 9.3, 12.8, MUTED, after=8)
    para("Informatikstudent mit Projekterfahrung in Java und Webentwicklung.", 9.9, 13.8, after=0)
    section("Studium")
    item("Bachelorstudium Informatik", "2024 - voraussichtlich 09/2027")
    para("Hochschule Musterstadt", after=1)
    section("Ausgewählte Projekte")
    item("QuizArena · Multiplayer-Quizplattform", "2025 - 2026")
    para("Java · Vert.x · REST · MQTT · MariaDB", 9.0, 11.9, MUTED, after=3)
    bullet("Entwicklung einer Client-Server-Anwendung mit Java-Backend.")
    item("Stream Club · Frontend-Prototyp")
    para("React · TypeScript · HTML · CSS", 9.0, 11.9, MUTED, after=3)
    bullet("Responsives Dashboard mit wiederverwendbaren Komponenten.")
    item("Datenanalyse und Machine Learning", "2026")
    bullet("Bildklassifikation mit PyTorch und CNNs.")
    section("Technische Kompetenzen")
    para("<b>Programmierung:</b> Java, JavaScript, TypeScript, Python", after=2)
    section("Sprachen")
    para("<b>Französisch</b> Muttersprache · <b>Deutsch</b> Sehr gut", after=0)


def make_letter():
    pass


if __name__ == "__main__":
    make_cv()
