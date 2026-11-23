

def test_overview_hero_puts_binding_first_when_not_bound():
    """2026-11-20 owner：「绑定等关键操作不够突出」。

    没绑定时，总览 Hero 的第一颗按钮必须是「绑定这台电脑」且为 primary；
    已绑定则不许再出现这颗按钮（避免误导为「还没绑」）。
    """
    from djb_node.desktop import datasource as ds
    from djb_node.desktop import views as vw

    sections = {name: ds.Unavailable("测试") for name in ds.SECTIONS}
    sections["account"] = {"profile": {"email": "a@b.c"}}
    sections["access"] = {"binding": {"state": "unbound"}}
    view = vw.build_view("overview", ds.Snapshot(sections))
    hero = view.hero
    assert hero is not None
    first = hero.actions[0]
    assert "绑定这台电脑" in first.label
    assert first.primary is True and first.nav == "account"
    assert "绑定后才能接单" in hero.hint

    sections2 = dict(sections)
    sections2["access"] = {"binding": {"state": "bound", "node_id": "n_x"}}
    view2 = vw.build_view("overview", ds.Snapshot(sections2))
    labels = [action.label for action in view2.hero.actions]
    assert not any("绑定这台电脑" in label for label in labels)