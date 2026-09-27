# Security and privacy

BewerbungsPilot separates publishable source code from operational application data.

## Never commit

- real candidate profiles and addresses;
- CVs, cover letters and certificates generated for real applications;
- completed application forms and screenshots;
- browser profiles, cookies, sessions or authentication data;
- portal receipts and confirmation e-mails;
- technical logs containing application context;
- `.env` files, tokens or credentials.

The default `.gitignore` excludes the operational directories used by the project. A public deployment should still run a secret scan before every release and should store private data outside the repository whenever possible.

## Human approval

Preparing a dossier and sending it are separate actions. A submission requires an authorization tied to one application and one execution. The authorization is invalidated if the form or document set changes.

## Recovery

The submission attempt is written before the browser click. If the result is uncertain, the runner must inspect the portal or confirmation channel before considering another attempt. It must never retry a consequential submission blindly.

## Public fixtures

All profiles, employers and offers included in the test suite are fictional. They exist to exercise the workflow without exposing a real person's application history.
