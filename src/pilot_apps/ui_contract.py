"""Reject native form submission unsupported by the opaque preview sandbox."""

from html.parser import HTMLParser


class PreviewControls(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.forms += 1
        kind = (attrs.get("type") or "").lower()
        if (
            tag == "button"
            and (kind == "submit" or (not kind and (self.forms or attrs.get("form"))))
        ) or (tag == "input" and kind in {"submit", "image"}):
            raise ValueError(
                "Preview cannot submit native forms. Use type=button and a JavaScript click handler with the registered model message bridge; do not expand sandbox permissions."
            )

    def handle_endtag(self, tag):
        if tag == "form":
            self.forms = max(0, self.forms - 1)


def check_preview_controls(html):
    PreviewControls().feed(html)


INTERACTION_GUIDE = """The preview's allow-scripts sandbox deliberately disallows native form submissions. Use explicit type=button and click handlers for model requests, even inside a form. Never depend on a submit event and never request allow-forms, allow-same-origin or a broader sandbox. Display stop/length as user-friendly completion or partial-answer status, not receipt IDs or raw protocol codes."""
