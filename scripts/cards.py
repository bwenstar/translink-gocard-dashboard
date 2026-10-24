"""Which go cards are tracked, and where each card's data lives.

Card 1 is USERNAME / PASSWORD in .env, as it always was. Add more cards by numbering them:

  USERNAME_2=<16-digit card number>
  PASSWORD_2=<its go card online password>
  CARD_NAME_2=Partner          # optional label for the dashboard (default "Card 2")

Card 1's files keep their original places (data/history.csv, data/raw/); card N gets
data/history.cardN.csv and data/raw-cardN/. Each card's history stays separate because
the go card export doesn't say which card a row came from.
"""
import dataclasses
import pathlib

MAX_CARDS = 9


@dataclasses.dataclass
class Card:
    n: int
    label: str
    history: pathlib.Path
    raw: pathlib.Path

    @property
    def suffix(self):
        return suffix(self.n)


def suffix(n):
    """The .env key suffix for card n: "" for card 1, "_2" for card 2."""
    return "" if n == 1 else f"_{n}"


def read_env(path):
    vals = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    return vals


def paths(n, data_dir):
    """(history, raw) for card n under data_dir."""
    data_dir = pathlib.Path(data_dir)
    if n == 1:
        return data_dir / "history.csv", data_dir / "raw"
    return data_dir / f"history.card{n}.csv", data_dir / f"raw-card{n}"


def label(n, env):
    return env.get(f"CARD_NAME{suffix(n)}") or ("Card 1" if n == 1 else f"Card {n}")


def configured(env, data_dir):
    """Cards with credentials in .env: card 1 always, then every numbered card present."""
    out = []
    for n in range(1, MAX_CARDS + 1):
        if n == 1 or env.get(f"USERNAME{suffix(n)}") or env.get(f"GOCARD_CARD{suffix(n)}"):
            out.append(Card(n, label(n, env), *paths(n, data_dir)))
    return out


def present(env, data_dir):
    """Cards that have a history file to build from (for build.py; needs no credentials)."""
    out = []
    for n in range(1, MAX_CARDS + 1):
        history, raw = paths(n, data_dir)
        if history.exists():
            out.append(Card(n, label(n, env), history, raw))
    return out
