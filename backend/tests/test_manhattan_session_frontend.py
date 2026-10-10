"""曼哈顿 Cookie 面板的前端契约：来源/保存时间展示与「验证即保存」文案。"""

from pathlib import Path


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_manhattan_session_dialog_shows_where_the_cookie_comes_from() -> None:
    assert "manhattanSessionSourceLabel: function()" in FRONTEND
    assert (
        "mh: {loggedIn:false, source:'', persisted:false, updatedAt:'', updatedBy:''},"
        in FRONTEND
    )
    assert "（{{manhattanSessionSourceLabel()}}）" in FRONTEND
    assert "保存时间：{{mh.updatedAt}}" in FRONTEND
    assert "已保存到服务器，容器重启后仍有效" in FRONTEND
    assert "来自服务器环境变量 NMHT_COOKIE" in FRONTEND


def test_manhattan_session_paste_is_saved_and_cleared_explicitly() -> None:
    assert "该 Cookie 同时用于盲标建单前的工单号真实性校验" in FRONTEND
    assert "验证通过后会保存到服务器数据卷，容器重启或重新部署后仍然有效" in FRONTEND
    assert "d.persisted === false" in FRONTEND
    assert "self.mht.cookie = '';" in FRONTEND
    assert "fetch(API + '/manhattan/session', {method:'DELETE'" in FRONTEND
    assert 'placeholder="粘贴曼哈顿后台 Cookie"' in FRONTEND
    assert "'验证并保存'" in FRONTEND
