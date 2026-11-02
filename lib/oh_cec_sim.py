"""BK-808 source-CEC stand-in: records requests without changing AV state."""
from __future__ import annotations

import copy
from oh_matrix_sim import BK808, Server


class Matrix(BK808):
    def __init__(self, input_: int = 4):
        super().__init__()
        self.input = input_
        self.commands: list[int] = []
        self.before = copy.deepcopy(self.status)

    def handlers(self):
        return {"get video status": super().handlers()["get video status"], "cec command": self.cec}

    def cec(self, request: dict) -> dict:
        ports = [int(i == self.input - 1) for i in range(8)]
        if (set(request) != {"comhead", "language", "object", "port", "index"}
                or type(request["language"]) is not int or request["language"] != 0
                or type(request["object"]) is not int or request["object"] != 0
                or request["port"] != ports or any(type(value) is not int for value in request["port"])
                or type(request["index"]) is not int or request["index"] not in range(1, 17)):
            self.unknown.append("invalid source CEC request")
            return {"comhead": "cec command", "result": 0}
        self.commands.append(request["index"])
        return {"comhead": "cec command", "result": 1}
