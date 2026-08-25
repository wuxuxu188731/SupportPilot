import pytest

from app.tools.composite_gateway import CompositeToolGateway


class Gateway:
    def __init__(self, *names):
        self.names = names

    @property
    def definitions(self):
        return [
            {"type": "function", "function": {"name": name}}
            for name in self.names
        ]

    def bind(self, *, context):
        return {name: (lambda **kwargs: kwargs) for name in self.names}


def test_composite_preserves_definition_order_and_merges_bindings():
    composite = CompositeToolGateway(
        [Gateway("a", "b", "c", "d"), Gateway("search_knowledge")]
    )
    assert [
        item["function"]["name"] for item in composite.definitions
    ] == ["a", "b", "c", "d", "search_knowledge"]
    assert set(composite.bind(context=object())) == {
        "a", "b", "c", "d", "search_knowledge"
    }


def test_composite_rejects_duplicate_tool_names():
    with pytest.raises(ValueError, match="duplicate tool name"):
        CompositeToolGateway([Gateway("same"), Gateway("same")])
