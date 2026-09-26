from tg_claude.rich import rich_to_text

RICH = {"blocks": [
    {"type": "paragraph", "text": "CI скипаем."},
    {"type": "list", "items": [
        {"label": "1.", "blocks": [{"type": "paragraph", "text": "адреса верные"}], "value": 1, "type": "1"},
        {"label": "2.", "blocks": [
            {"type": "paragraph", "text": "стейджи — переменные"},
            {"type": "list", "items": [{"label": "•", "blocks": [{"type": "paragraph", "text": "вложенный"}]}]},
        ], "value": 2, "type": "1"},
    ]},
]}


def test_rich_to_text():
    assert rich_to_text(RICH) == "CI скипаем.\n\n1. адреса верные\n2. стейджи — переменные\n   - вложенный"


def test_unknown_blocks_fall_back_to_text():
    assert rich_to_text({"blocks": [{"type": "something_new", "text": "hi"}]}) == "hi"
    assert rich_to_text({"blocks": []}) == ""
