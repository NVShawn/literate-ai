# Lab authoring support

Attendees build and run a lab with `litai build` and `litai run` from its
independent project directory. See [the labs guide](../README.md).

The scripts here maintain the authored offline previews and exercise their source
inspector. They do not replace Standard lifecycle admission or issue receipts.
The framework owns `docs/`; the labs are independent projects. Each lab's
authority and agent entry points are validated separately, while the website
verifier checks links across the full attendee kit. Framework documentation size
limits do not classify a lab's build products or caches as documentation assets.
From the framework repository root, maintainers can assemble and check previews:

```sh
python3 labs/scripts/build_labs.py
python3 labs/scripts/verify_labs.py
python3 labs/scripts/test_lab_sources.py
node labs/scripts/test_labs_browser.cjs
```

The browser checks require Playwright and Chromium. Captures belong in ignored
`_build/`. `author_retained_inputs.py` prepares explicitly untrusted initial source
with the installed 1.3.1 framework APIs; only an acknowledged public `litai rebuild`
can qualify it. Writing test metadata or an SBOM does not establish acceptance.
