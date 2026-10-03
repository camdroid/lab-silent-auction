"""In-memory stand-in for the parts of gspread the app uses."""

import re


def _a1(ref: str) -> tuple[int, int]:
    m = re.fullmatch(r"([A-Z]+)(\d+)", ref)
    col = 0
    for ch in m.group(1):
        col = col * 26 + ord(ch) - 64
    return int(m.group(2)) - 1, col - 1


class FakeWorksheet:
    _next_id = 100

    def __init__(self, title, rows=None):
        self.title, self.rows = title, [list(r) for r in (rows or [])]
        FakeWorksheet._next_id += 1
        self.id = FakeWorksheet._next_id

    def get_all_values(self):
        width = max((len(r) for r in self.rows), default=0)
        return [[("" if v is None else v if isinstance(v, str) else str(v).upper() if isinstance(v, bool) else str(v))
                 for v in r] + [""] * (width - len(r)) for r in self.rows]

    def append_row(self, values, value_input_option=None, table_range=None):
        self.rows.append(list(values))

    def batch_update(self, data, value_input_option=None):
        for d in data:
            r, c = _a1(d["range"])
            while len(self.rows) <= r:
                self.rows.append([])
            row = self.rows[r]
            while len(row) <= c:
                row.append("")
            row[c] = d["values"][0][0]


class FakeSpreadsheet:
    def __init__(self, tabs: dict):
        self.tabs = [FakeWorksheet(t, rows) for t, rows in tabs.items()]
        self.requests = []
        self.reads = 0

    def worksheets(self):
        return list(self.tabs)

    def add_worksheet(self, title, rows, cols):
        ws = FakeWorksheet(title)
        self.tabs.append(ws)
        return ws

    def batch_update(self, body):
        self.requests.extend(body["requests"])

    def values_batch_get(self, ranges):
        self.reads += 1
        out = []
        for rng in ranges:
            m = re.fullmatch(r"'((?:[^']|'')+)'(?:!(\d+):(\d+))?", rng)
            title = m.group(1).replace("''", "'")
            ws = next(w for w in self.tabs if w.title == title)
            vals = ws.get_all_values()
            if m.group(2):
                vals = vals[int(m.group(2)) - 1:int(m.group(3))]
            # The real API drops trailing empty cells.
            vals = [r[:max((i + 1 for i, v in enumerate(r) if v != ""), default=0)] for r in vals]
            out.append({"range": rng, "values": vals})
        return {"valueRanges": out}
