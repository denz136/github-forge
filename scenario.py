"""Port of the suite's seeded scenario (program/env/src/wire.ts) to Python.

The suite derives every name it sends from one fixed seed (SCENARIO_SEED). The
forge only needs the user list: the GraphQL viewer-login check and the REST
ownership model must agree with the token owner's login, which is the
scenario's first generated user.
"""

U64_MASK = (1 << 64) - 1
SCENARIO_SEED = 0x6A09E667F3BCC909

FIRST_NAMES = [
    'ada', 'bram', 'cora', 'dmitri', 'elena', 'felix', 'greta', 'hugo', 'iris', 'jonas', 'kira', 'lars',
    'mira', 'nico', 'olga', 'pavel', 'quinn', 'rosa', 'sven', 'tess', 'ulla', 'vito', 'wren', 'yara', 'zane',
]
LAST_NAMES = [
    'anders', 'bauer', 'costa', 'dahl', 'eriksen', 'fischer', 'garcia', 'holm', 'iversen', 'jansen', 'keller',
    'lind', 'moreau', 'novak', 'olsen', 'peters', 'quist', 'rossi', 'sato', 'turi', 'ulrich', 'vega', 'weber',
    'xu', 'yilmaz', 'zimmer',
]


def fnv1a64(text):
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & U64_MASK
    return h


def mix64(value):
    z = (value + 0x9E3779B97F4A7C15) & U64_MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & U64_MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & U64_MASK
    return (z ^ (z >> 31)) & U64_MASK


def scenario_seed_for(label):
    return mix64(SCENARIO_SEED ^ fnv1a64(label))


class Rng:
    """xorshift64* with splitmix64 seeding, identical to wire.ts's Rng."""

    def __init__(self, seed):
        state = seed & U64_MASK
        self.state = SCENARIO_SEED if state == 0 else state

    def next_u64(self):
        x = self.state
        x = (x ^ (x >> 12)) & U64_MASK
        x = (x ^ (x << 25)) & U64_MASK
        x = (x ^ (x >> 27)) & U64_MASK
        self.state = x
        return (x * 0x2545F4914F6CDD1D) & U64_MASK

    def next_u32(self):
        return self.next_u64() >> 32

    def next_int(self, max_exclusive):
        if max_exclusive <= 0:
            raise ValueError('nextInt needs a positive integer')
        if max_exclusive == 1:
            return 0
        limit = (1 << 32) - ((1 << 32) % max_exclusive)
        while True:
            value = self.next_u32()
            if value < limit:
                return value % max_exclusive


def generate_users(rng, count):
    used = set()
    users = []
    for i in range(count):
        first = FIRST_NAMES[rng.next_int(len(FIRST_NAMES))]
        last = LAST_NAMES[rng.next_int(len(LAST_NAMES))]
        base = ''.join(c for c in first + last if c.isalnum() or c == '-')
        login = base
        suffix = 2
        while login in used:
            login = f'{base}-{suffix}'
            suffix += 1
        used.add(login)
        users.append({'id': 1000 + i, 'login': login, 'name': f'{first} {last}',
                      'email': f'{login}@example.invalid'})
    return users


def scenario_viewer_login():
    rng = Rng(scenario_seed_for('users'))
    return generate_users(rng, 12)[0]['login']


VIEWER_LOGIN = scenario_viewer_login()
