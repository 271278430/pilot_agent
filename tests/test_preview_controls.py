import pytest

from pilot_apps.ui_contract import check_preview_controls


@pytest.mark.parametrize(
    "html",
    [
        '<form><button type="submit">ask</button></form>',
        "<form><button>ask</button></form>",
        '<button type="submit" form="chat">ask</button>',
        '<input type="submit">',
        '<form><input type="image"></form>',
    ],
)
def test_unsupported_native_submission_is_rejected_before_deployment(html):
    with pytest.raises(ValueError, match="do not expand sandbox"):
        check_preview_controls(html)


def test_explicit_script_controls_do_not_need_broader_sandbox():
    check_preview_controls(
        '<form><textarea></textarea><button type="button">ask</button></form><script>button.addEventListener("click", send)</script>'
    )
