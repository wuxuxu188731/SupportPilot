from typing import Any, Protocol


class ToolGateway(Protocol):
    @property
    def definitions(self) -> list[dict]:
        raise NotImplementedError

    def bind(self, *, context: Any) -> dict:
        raise NotImplementedError


class CompositeToolGateway:
    def __init__(self, gateways: list[ToolGateway]) -> None:
        self._gateways = list(gateways)
        names: set[str] = set()
        for definition in self.definitions:
            name = definition["function"]["name"]
            if name in names:
                raise ValueError(f"duplicate tool name: {name}")
            names.add(name)

    @property
    def definitions(self) -> list[dict]:
        return [
            definition
            for gateway in self._gateways
            for definition in gateway.definitions
        ]

    def bind(self, *, context: Any) -> dict:
        functions = {}
        for gateway in self._gateways:
            bound = gateway.bind(context=context)
            overlap = functions.keys() & bound.keys()
            if overlap:
                raise ValueError(
                    f"duplicate tool name: {sorted(overlap)[0]}"
                )
            functions.update(bound)
        return functions
