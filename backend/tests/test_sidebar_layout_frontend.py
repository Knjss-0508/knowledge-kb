from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_desktop_sidebar_is_fixed_outside_the_document_scroll_flow() -> None:
    assert ":root{--sidebar-width:236px}" in FRONTEND
    assert "html,body{min-height:100%}" in FRONTEND
    assert ".layout{display:block;min-height:100vh}" in FRONTEND
    assert (
        ".side{position:fixed;top:0;left:0;z-index:300;"
        "width:var(--sidebar-width);min-width:var(--sidebar-width);"
        "max-width:var(--sidebar-width);height:100vh;height:100dvh;"
        "min-height:0;max-height:100vh;max-height:100dvh"
        in FRONTEND
    )
    assert (
        ".main{width:calc(100% - var(--sidebar-width));"
        "min-width:0;margin-left:var(--sidebar-width)}"
        in FRONTEND
    )
    assert "html,body{height:100%;overflow:hidden}" not in FRONTEND


def test_mobile_layout_returns_sidebar_to_normal_document_flow() -> None:
    assert (
        "@media(max-width:760px){.layout{min-height:100vh}"
        ".side{position:static;top:auto;left:auto;z-index:auto;"
        "min-width:0;max-width:none;height:auto;max-height:none;"
        "overflow:visible}.main{width:100%;min-height:0;margin-left:0}}"
        in FRONTEND
    )
