#!/usr/bin/env python3
#
# no_exit.py — generate no_exit.plot: snakes looking for a way out of a box
# that has none.
#
# Everything sits on a square grid whose pitch is chosen so that lines on
# neighbouring grid lines just don't quite touch. Every grid node is used by at
# most one line, so nothing ever crosses or overlaps.
#
#   * The border is 4 grey lines, one grid step apart, concentric with the
#     edge: the outermost is a single solid line. Running through them are a
#     number of dead-end voids. Each opens through a gap in the innermost line
#     and runs along the lines between, stepping across ONE line at a time and
#     turning after every step, never back into the snake space, and stops
#     blind. None reaches the outside.
#   * Snakes spawn at random free points inside. Each moves a random distance
#     along x or y, then turns left or right, and so on. A snake that reaches a
#     gap goes in, follows the passage to its end and stops: that exit is full.
#     A snake that is boxed in, or reaches the end of its life, stops where it
#     is. Snakes keep coming until every exit is full.
#
# Snakes find the exits without any steering: one that runs into the inner
# wall can only turn and slide along it, and sliding along the wall is how it
# passes the gaps. The node just inside each gap is reserved for the snake
# that will enter, so no snake can block an exit by lying across its mouth.
#
# Usage:
#   ./no_exit.py                       # seed 1, light pen, 5 mm pitch
#   ./no_exit.py --seed 7 --exits 10
#   ./no_exit.py --ink --pitch 1.5     # ink pen, finer grid
#   ./no_exit.py --no-walls --out no_exit_nowalls   # the snakes alone
# Writes no_exit.plot and no_exit_preview.png (or --out NAME).
#
# Coordinates: A4 landscape (297 x 210 mm), lower-left corner at --origin
# (default 5,47: the xmin,ymax corner, away from the camera glow at the home corner; 48,26 centres it). With paper calibrated in
# plotter.pl (`set min` at the sheet's corner), use --origin 0,0.

import argparse
import colorsys
import math
import random
from collections import deque

from PIL import Image, ImageDraw
import outdirs as OD

A4 = (297.0, 210.0)
BORDER = 3                      # innermost border line's depth: 4 lines, depths 0..3
DIRS = [(1, 0), (0, 1), (-1, 0), (0, -1)]


class World:
    def __init__(self, W, H, rng):
        self.W, self.H, self.rng = W, H, rng
        self.corr = {}          # node -> exit index, for passage nodes
        self.exits = []         # dicts: gap, approach, path (gap first), full
        self.snake_at = {}      # node -> snake index
        self.snakes = []        # dicts: path, found (bool)

    def depth(self, n):
        i, j = n
        return min(i, j, self.W - 1 - i, self.H - 1 - j)

    def inside(self, n):
        return 0 <= n[0] < self.W and 0 <= n[1] < self.H

    # -- the dead-end passages ------------------------------------------------

    def outward(self, g):
        """Unit step from an inner-wall node towards the outside."""
        i, j = g
        d = self.depth(g)
        if i == d:
            return (-1, 0)
        if self.W - 1 - i == d:
            return (1, 0)
        if j == d:
            return (0, -1)
        return (0, 1)

    def far_from_corridors(self, n, own, recent):
        """Traps are one grid line wide and never meet. Anything that isn't
        this trap's own last few nodes must be >= 3 steps away (Chebyshev) —
        other traps AND this trap's own earlier stretch. With only 2, the
        single wall node between has no wall neighbour along its line, so no
        edge is drawn there: two traps would run together, or a trap that
        doubles back past one wall node reads as a channel two lines wide.
        The last 3 nodes are exempt — a staircase turn puts them closer."""
        for di in (-2, -1, 0, 1, 2):
            for dj in (-2, -1, 0, 1, 2):
                m = (n[0] + di, n[1] + dj)
                if m in self.corr:
                    return False
                if m in own and m not in recent:
                    return False
        return True

    def dig(self, min_len, max_len, tries=400):
        """One passage (a void through the fill): a gap in the innermost line,
        then a walk along the border lines. Two rules shape it:
          * it crosses only ONE line at a time: every step across the band
            (to the next line in or out) is followed by a turn to run along
            the line it has reached — so a void climbs the band like a stair;
          * it never re-enters the snake space: past its own gap it stays on
            the lines strictly between the outer line and the innermost one.
        It must also keep one line of fill between itself and every other void,
        and between itself and its own earlier stretch."""
        rng = self.rng
        lo_d, hi_d = 1, BORDER - 1               # the lines a void may run along
        ring = [n for n in self.ring_nodes(BORDER) if self.not_near_corner(n)]
        for _ in range(tries):
            g = rng.choice(ring)
            if any(max(abs(g[0] - m[0]), abs(g[1] - m[1])) < 4 for m in self.corr):
                continue
            out = self.outward(g)
            first = (g[0] + out[0], g[1] + out[1])
            if not (lo_d <= self.depth(first) <= hi_d):
                continue
            path, own = [g, first], {g, first}
            target = rng.randint(min_len, max_len)
            crossed = True                       # entering crossed a line: turn next
            run, run_len = 0, rng.randint(2, 8)
            while len(path) < target:
                cur, prev = path[-1], path[-2]
                d0 = self.depth(cur)
                along, across = [], []
                for d in DIRS:
                    n = (cur[0] + d[0], cur[1] + d[1])
                    if n == prev or n in own or not self.inside(n):
                        continue
                    dn = self.depth(n)
                    if dn == d0:
                        along.append(n)
                    elif lo_d <= dn <= hi_d:
                        across.append(n)
                rng.shuffle(along)
                # after crossing a line, run along it; after a run, cross one
                if crossed or not across or run < run_len:
                    opts = along
                else:
                    opts = across + along
                moved = None
                for n in opts:
                    if self.far_from_corridors(n, own, set(path[-3:])):
                        moved = n
                        break
                if moved is None:
                    break
                crossed = self.depth(moved) != d0
                if crossed:
                    run, run_len = 0, rng.randint(2, 8)
                else:
                    run += 1
                path.append(moved)
                own.add(moved)
            if len(path) < min_len:
                continue
            k = len(self.exits)
            for n in path:
                self.corr[n] = k
            self.exits.append(dict(gap=g, approach=(g[0] - out[0], g[1] - out[1]),
                                   path=path, full=False))
            return True
        return False

    def ring_nodes(self, d):
        W, H = self.W, self.H
        out = []
        for i in range(d, W - d):
            out += [(i, d), (i, H - 1 - d)]
        for j in range(d + 1, H - 1 - d):
            out += [(d, j), (W - 1 - d, j)]
        return out

    def not_near_corner(self, n):
        """At least 3 steps from every corner of the inner wall: a gap at a
        corner would open on two sides at once."""
        d = BORDER
        cs = [(d, d), (self.W - 1 - d, d), (d, self.H - 1 - d), (self.W - 1 - d, self.H - 1 - d)]
        return all(max(abs(n[0] - c[0]), abs(n[1] - c[1])) >= 3 for c in cs)

    # -- walls ------------------------------------------------------------------

    def wall_edges(self):
        """Grid edges to draw: the whole band filled with grey, except the
        passages. Every band node that is not a passage node is wall.

        Ring edges join neighbours at the same depth, so the fill runs as
        concentric lines parallel to the border — the outer one (depth 0) solid,
        the inner wall (depth BORDER) broken only by the gaps. A strip of wall
        between two legs of a passage that runs radially has no ring neighbour
        to join, and would vanish; those nodes are joined radially instead, so
        every passage stays walled on both sides."""
        E = set()

        def add(a, b):
            E.add((a, b) if a <= b else (b, a))

        def wall(n):
            return self.inside(n) and self.depth(n) <= BORDER and n not in self.corr

        ring_joined = set()
        for i in range(self.W):
            for j in range(self.H):
                a = (i, j)
                if not wall(a):
                    continue
                for dd in ((1, 0), (0, 1)):
                    b = (i + dd[0], j + dd[1])
                    if wall(b) and self.depth(a) == self.depth(b):
                        add(a, b)
                        ring_joined.update((a, b))
        for i in range(self.W):
            for j in range(self.H):
                a = (i, j)
                if not wall(a) or a in ring_joined:
                    continue
                for dd in ((1, 0), (0, 1)):
                    b = (i + dd[0], j + dd[1])
                    if wall(b) and b not in ring_joined:
                        add(a, b)
        return E

    # -- snakes -----------------------------------------------------------------

    def open_exit_at(self, n):
        for e in self.exits:
            if not e['full'] and e['approach'] == n:
                return e
        return None

    def free(self, n):
        if not self.inside(n) or self.depth(n) <= BORDER or n in self.snake_at:
            return False
        if any(e['approach'] == n for e in self.exits):
            return self.open_exit_at(n) is not None     # only while it is open
        return True

    def reachable_open_exit(self, start):
        """BFS through free nodes: can a snake starting here reach an open exit?"""
        seen, q = {start}, deque([start])
        while q:
            n = q.popleft()
            if self.open_exit_at(n):
                return True
            for d in DIRS:
                m = (n[0] + d[0], n[1] + d[1])
                if m not in seen and self.free(m):
                    seen.add(m)
                    q.append(m)
        return False

    def open_blocks(self, k=3):
        """Lower-left corners of every free k x k block of nodes in the arena.
        The fill ends when there are none."""
        out = []
        for i in range(BORDER + 1, self.W - BORDER - k):
            for j in range(BORDER + 1, self.H - BORDER - k):
                if all(self.free((i + a, j + b)) and not self.open_exit_at((i + a, j + b))
                       for a in range(k) for b in range(k)):
                    out.append((i, j))
        return out

    def free_degree(self, n):
        return sum(self.free((n[0] + d[0], n[1] + d[1])) for d in DIRS)

    def spawn_fill(self, k):
        """Phase-2 spawn: the leftover space is narrow channels, so start a
        fill snake at the END of one — of the nodes in any free k x k block,
        one with the fewest free neighbours — heading into the open."""
        cand = {(i + a, j + b) for (i, j) in self.open_blocks(k)
                for a in range(k) for b in range(k)}
        if not cand:
            return None, None
        lo = min(self.free_degree(n) for n in cand)
        n = self.rng.choice(sorted(n for n in cand if self.free_degree(n) == lo))
        dirs = [d for d in DIRS if self.free((n[0] + d[0], n[1] + d[1]))]
        return n, self.rng.choice(dirs)

    def run_fill_snake(self, start, heading, max_run, prefix=None, kind='fill'):
        """A fill snake meanders like the others — a random short run, then a
        random left or right — but never dies while it has somewhere to go:
        when the way it wants is taken it takes whichever way is open. (Going
        straight until blocked filled the space too, but curled every snake
        into a square spiral.) No length limit, so it works a leftover channel
        to the end."""
        rng = self.rng
        k = len(self.snakes)
        path = list(prefix or []) + [start]
        for m in path:
            self.snake_at[m] = k
        found = False
        run_left = rng.randint(1, max_run)
        while True:
            cur = path[-1]
            left, right = (-heading[1], heading[0]), (heading[1], -heading[0])
            turns = rng.sample([left, right], 2)
            options = [heading] + turns if run_left > 0 else turns + [heading]
            nxt = None
            for d in options:
                m = (cur[0] + d[0], cur[1] + d[1])
                if self.free(m):
                    if d != heading:
                        run_left = rng.randint(1, max_run)
                    nxt, heading = m, d
                    break
            if nxt is None:
                break
            run_left -= 1
            path.append(nxt)
            self.snake_at[nxt] = k
            e = self.open_exit_at(nxt)
            if e:
                e['full'] = True
                for m in e['path']:
                    path.append(m)
                    self.snake_at[m] = k
                found = True
                break
        self.snakes.append(dict(path=path, found=found, kind=kind))
        return found

    def escape(self, e, max_run):
        """A trap no snake found: a snake starts at its blind end, follows the
        trap back out through its gap, and meanders on into the arena as far
        as it can. The trap's mouth was reserved for an entering snake, so it
        is always free for this one to come out through."""
        out = [n for n in reversed(e['path'])]          # blind end ... gap
        a = e['approach']
        e['full'] = True
        g = e['path'][0]
        heading = (a[0] - g[0], a[1] - g[1])            # out of the gap, into the arena
        self.run_fill_snake(a, heading, max_run, prefix=out, kind='escape')

    def spawn_any(self):
        """Last sweep: any free node with a free neighbour can take a 2-node
        snake. Start at a channel end (fewest free neighbours), as for fill."""
        cand = [(i, j) for i in range(BORDER + 1, self.W - BORDER - 1)
                for j in range(BORDER + 1, self.H - BORDER - 1)
                if self.free((i, j)) and self.free_degree((i, j)) > 0]
        if not cand:
            return None, None
        lo = min(self.free_degree(n) for n in cand)
        n = self.rng.choice(sorted(n for n in cand if self.free_degree(n) == lo))
        dirs = [d for d in DIRS if self.free((n[0] + d[0], n[1] + d[1]))]
        return n, self.rng.choice(dirs)

    def spawn(self, tries=300):
        rng = self.rng
        for _ in range(tries):
            n = (rng.randrange(BORDER + 1, self.W - BORDER - 1),
                 rng.randrange(BORDER + 1, self.H - BORDER - 1))
            if not self.free(n) or self.open_exit_at(n):
                continue
            if not self.reachable_open_exit(n):
                continue
            dirs = [d for d in DIRS if self.free((n[0] + d[0], n[1] + d[1]))]
            if dirs:
                return n, rng.choice(dirs)
        # Random tries miss small pockets late on. Search exhaustively: every
        # free node that can still reach an open exit is a valid spawn, so the
        # plot only ends when no open exit can be reached from anywhere.
        cands = []
        for e in self.exits:
            if e['full']:
                continue
            seen, q = {e['approach']}, deque([e['approach']])
            while q:
                m = q.popleft()
                for d in DIRS:
                    x = (m[0] + d[0], m[1] + d[1])
                    if x not in seen and self.free(x):
                        seen.add(x)
                        q.append(x)
            cands += [m for m in seen if not self.open_exit_at(m)]
        rng.shuffle(cands)
        for n in cands:
            dirs = [d for d in DIRS if self.free((n[0] + d[0], n[1] + d[1]))]
            if dirs:
                return n, rng.choice(dirs)
        return None, None

    def run_snake(self, start, heading, life, max_run):
        rng = self.rng
        k = len(self.snakes)
        path = [start]
        self.snake_at[start] = k
        found = False
        while len(path) < life:
            steps = rng.randint(1, max_run)
            for _ in range(steps):
                cur = path[-1]
                nxt = (cur[0] + heading[0], cur[1] + heading[1])
                if not self.free(nxt):
                    break
                path.append(nxt)
                self.snake_at[nxt] = k
                e = self.open_exit_at(nxt)
                if e:                               # a way out! in it goes
                    e['full'] = True
                    for m in e['path']:
                        path.append(m)
                        self.snake_at[m] = k
                    found = True
                    break
            if found:
                break
            cur = path[-1]
            left, right = (-heading[1], heading[0]), (heading[1], -heading[0])
            turns = [t for t in rng.sample([left, right], 2)
                     if self.free((cur[0] + t[0], cur[1] + t[1]))]
            if not turns:
                break                               # boxed in
            heading = turns[0]
        self.snakes.append(dict(path=path, found=found))
        return found


# ---------------------------------------------------------------------------
# Paths -> drawing
# ---------------------------------------------------------------------------

def corners(path):
    """Keep a polyline's end points and turns only."""
    if len(path) < 3:
        return list(path)
    out = [path[0]]
    for a, b, c in zip(path, path[1:], path[2:]):
        if (b[0] - a[0], b[1] - a[1]) != (c[0] - b[0], c[1] - b[1]):
            out.append(b)
    out.append(path[-1])
    return out


def edges_to_polylines(E):
    """Chain grid edges into as few polylines as possible."""
    adj = {}
    for a, b in E:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    used, lines = set(), []

    def walk(a):
        line = [a]
        while True:
            nxt = None
            # prefer going straight, so polylines don't zig-zag at junctions
            prev = line[-2] if len(line) > 1 else None
            cands = [b for b in adj[line[-1]] if (min(line[-1], b), max(line[-1], b)) not in used]
            if not cands:
                return line
            if prev:
                d = (line[-1][0] - prev[0], line[-1][1] - prev[1])
                straight = [b for b in cands if (b[0] - line[-1][0], b[1] - line[-1][1]) == d]
                nxt = straight[0] if straight else cands[0]
            else:
                nxt = cands[0]
            used.add((min(line[-1], nxt), max(line[-1], nxt)))
            line.append(nxt)

    for start in sorted(adj, key=lambda n: len(adj[n]) % 2 == 0):   # odd-degree ends first
        while any((min(start, b), max(start, b)) not in used for b in adj[start]):
            lines.append(walk(start))
    return lines


def order_paths(items, start=(0, 0)):
    """Greedy nearest-neighbour ordering of (points, meta) to cut air moves.
    Paths may be reversed, except snakes, which are drawn head-last."""
    left, out, at = list(items), [], start
    while left:
        best = None
        for idx, (pts, meta) in enumerate(left):
            for rev in ((False, True) if meta.get('reversible') else (False,)):
                p = pts[-1] if rev else pts[0]
                d = (p[0] - at[0]) ** 2 + (p[1] - at[1]) ** 2
                if best is None or d < best[0]:
                    best = (d, idx, rev)
        _, idx, rev = best
        pts, meta = left.pop(idx)
        pts = pts[::-1] if rev else pts
        out.append((pts, meta))
        at = pts[-1]
    return out


def balanced(h):
    """The saturated colour of hue h (0-1), channels summing to 255, so every
    colour emits about the same light (two-channel hues would otherwise be
    twice as bright)."""
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, 1.0, 1.0)
    s = r + g + b
    return tuple(int(round(255 * c / s)) for c in (r, g, b))


# Deep, saturated colours from the rainbow rings that looked like glass tubes
# with the 8 mm APA-106 (2026-11-05, ED 1.6, red x0.7 and blue x0.65 so
# neither die clips in crescents): no greens, cyans or yellows, which go pale
# and clip first. Strongest channel at full drive; the level comes from paint.
TUBES = [(178, 0, 0),       # deep red
         (178, 60, 0),      # dark orange
         (178, 0, 110),     # pink-magenta
         (159, 0, 166),     # purple
         (79, 0, 166),      # violet
         (0, 0, 166),       # deep blue
         (0, 90, 166)]      # steel blue


# All 13 colours of rainbow_rings_red70_blue65.plot (rainbow_rings.py --red 0.7
# --blue 0.65), red outside to pink in the middle, greens and cyans included.
RINGS = [(178, 0, 0), (178, 113, 0), (178, 227, 0), (119, 255, 0), (40, 255, 0),
         (0, 255, 37), (0, 255, 110), (0, 227, 166), (0, 113, 166), (0, 0, 166),
         (79, 0, 166), (159, 0, 166), (178, 0, 110)]


def tube_colour(rng, palette=TUBES):
    """A snake colour from the palette, and the one 3 further on as its 'inverse'."""
    i = rng.randrange(len(palette))
    return palette[i], palette[(i + 3) % len(palette)]


def balanced_hue(rng):
    """A random snake colour, and its inverse: the opposite hue. A plain RGB
    inverse (255 - c) washes out the in-between hues (orange -> pale blue);
    the opposite hue keeps it saturated and equally bright."""
    h = rng.random()
    return balanced(h), balanced(h + 0.5)


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description='Generate the no_exit plot.')
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--pitch', type=float, default=None,
                    help='grid pitch, mm (default 5 for the light pen, 1.5 for ink)')
    ap.add_argument('--ink', action='store_true', help='ink pen: no colours')
    ap.add_argument('--exits', type=int, default=16)
    ap.add_argument('--wall-level', type=int, default=90,
                    help='border grey level (0-255; default 90: white LED step 3 at brightness 3 — '
                         '28/step 1 and 60/step 2 lacked presence). Grey (R=G=B) lights the carrier\'s own '
                         'WHITE element, one die not three')
    ap.add_argument('--wall-speed', type=int, default=3000,
                    help='border feed, mm/min (light pen; default 3000, matching what short '
                         'wall pieces average, so long stretches are not dimmer)')
    ap.add_argument('--life', default='40,160', help='snake length range, nodes (min,max)')
    ap.add_argument('--run', type=int, default=8, help='longest straight run, nodes')
    ap.add_argument('--max-snakes', type=int, default=5000)
    ap.add_argument('--fill', type=int, default=2,
                    help='keep filling until no free FILL x FILL block of nodes is left '
                         '(2: gaps of at most one node; 3: the looser fill)')
    ap.add_argument('--origin', default='5,47',
                    help="A4 sheet's lower-left corner, mm (default 5,47: the xmin,ymax corner, "
                         "5 mm in — diagonally away from the camera's corner glow, which is at the "
                         "xmax,ymin home corner; 48,26 centres it on the bed)")
    ap.add_argument('--dots', action='store_true',
                    help='mark each snake\'s birthplace with a dot of the opposite hue (light pen)')
    ap.add_argument('--dot', type=float, default=0.5,
                    help='birth-dot stroke length, mm (out and back)')
    ap.add_argument('--no-walls', action='store_true',
                    help='leave the border out of the plot and the preview: the snakes alone, '
                         'exactly as they are with it')
    ap.add_argument('--palette', choices=('balanced', 'tubes', 'rings'), default='balanced',
                    help="snake colours: 'balanced' (any hue, equal light), 'tubes' (the deep "
                         "rainbow-ring colours, for the 8 mm APA-106's glass-tube look) or 'rings' "
                         "(all 13 rainbow-ring colours)")
    ap.add_argument('--green', type=float, default=1.0,
                    help="scale on the snakes' green channel: with the 8 mm APA-106 the greens, "
                         "cyans and yellows clipped pale at ED 1.6 (no_exit_rings, 2026-11-05)")
    ap.add_argument('--red', type=float, default=1.0,
                    help="scale on the snakes' red channel: pure red was the brightest colour and "
                         "rendered flat-topped, featureless (2026-11-05)")
    ap.add_argument('--blue', type=float, default=1.0,
                    help="scale on the snakes' blue channel: the purples and violets clipped along "
                         "their full-speed straights at ED 1.6 (2026-11-05)")
    ap.add_argument('--wall-colour', default=None,
                    help='"R G B" for the border instead of --wall-level grey (the APA-106 mixes '
                         'grey from its three dies, which comes out lavender)')
    ap.add_argument('--dynamic', action='store_true',
                    help='dynamic brightness: everything at one speed (25000) with `dynamic on`, so '
                         'every millimetre gets the same light round the corners; the level then '
                         'comes from `paint -ed=`. UNPLUG THE SOLENOID')
    ap.add_argument('--sheet', default='297,210',
                    help='drawing area W,H mm (default A4 landscape; 373,242 is the bed less 10 mm)')
    ap.add_argument('--out', default='no_exit')
    a = ap.parse_args()
    sheet = tuple(float(v) for v in a.sheet.split(','))

    a.dots = a.dots and not a.ink
    pitch = a.pitch or (1.5 if a.ink else 5.0)
    ox, oy = (float(v) for v in a.origin.split(','))
    W, H = int(sheet[0] // pitch) + 1, int(sheet[1] // pitch) + 1
    # centre the grid on the sheet
    mx, my = ox + (sheet[0] - (W - 1) * pitch) / 2, oy + (sheet[1] - (H - 1) * pitch) / 2
    rng = random.Random(a.seed)
    w = World(W, H, rng)

    # Shorter traps when there are many, so they all fit round the band with
    # the 3-step spacing that keeps them from meeting.
    t_max = max(12, int(40 * 8 / max(a.exits, 1)) + 2)
    for _ in range(a.exits):
        # crowded band: if a full-length trap won't fit, settle for a short one
        if not (w.dig(min_len=6, max_len=t_max) or w.dig(min_len=5, max_len=10, tries=2000)):
            break
    lo, hi = (int(v) for v in a.life.split(','))
    # Phase 1: while any open trap can still be reached, random-walking snakes
    # spawn where they can reach one. Phase 2: fill snakes trace the leftover
    # channels until no free --fill x --fill block of nodes is left.
    fill_run = max(2, a.run // 2)
    # 1. Seekers: random walkers, spawned where an open trap is still reachable.
    while len(w.snakes) < a.max_snakes and not all(e['full'] for e in w.exits):
        n, h = w.spawn()
        if n is None:
            break
        w.run_snake(n, h, rng.randint(lo, hi), a.run)
    filled = sum(e['full'] for e in w.exits)
    # 2. Escapees: every trap no snake found sends one out into the arena.
    escaped = 0
    for e in w.exits:
        if not e['full']:
            w.escape(e, fill_run)
            escaped += 1
    # 3. Fill: meanderers until no free --fill x --fill block is left...
    while len(w.snakes) < a.max_snakes:
        n, h = w.spawn_fill(a.fill)
        if n is None:
            break
        w.run_fill_snake(n, h, fill_run)
    # 4. ...then a last sweep: any leftover space that can hold a 2-node snake.
    while len(w.snakes) < a.max_snakes:
        n, h = w.spawn_any()
        if n is None:
            break
        w.run_fill_snake(n, h, fill_run, kind='sweep')

    # Geometry in mm.
    P = lambda n: (round(mx + n[0] * pitch, 3), round(my + n[1] * pitch, 3))
    items = [([P(n) for n in corners(pl)], dict(kind='wall', reversible=True))
             for pl in edges_to_polylines(w.wall_edges())]
    walls = order_paths(items, start=P((W - 1, 0)))
    snakes = [([P(n) for n in corners(s['path'])],
               dict(kind='snake', found=s['found'], role=s.get('kind', 'seek'),
                    **dict(zip(('colour', 'inverse'),
                               tube_colour(rng, TUBES) if a.palette == 'tubes' else
                               tube_colour(rng, RINGS) if a.palette == 'rings' else
                               balanced_hue(rng)))))
              for s in w.snakes if len(s['path']) >= 2]
    if a.red != 1.0 or a.green != 1.0 or a.blue != 1.0:
        for _, m in snakes:
            for k in ('colour', 'inverse'):
                r_, g_, b_ = m[k]
                m[k] = (int(round(r_ * a.red)), int(round(g_ * a.green)), int(round(b_ * a.blue)))
    snakes = order_paths(snakes, start=walls[-1][0][-1] if walls else (0, 0))
    if a.no_walls:
        walls = []                   # after ordering, so the snakes run as they do with them

    # The plot file.
    mm = 0.0
    with open(OD.plot(f"{a.out}.plot"), 'w') as f:
        f.write(f"# {a.out}.plot — generated by no_exit.py --seed {a.seed} "
                f"--pitch {pitch:g}{' --ink' if a.ink else ''} --exits {a.exits}"
                f"{' --no-walls' if a.no_walls else ''}"
                f"{' --palette ' + a.palette if a.palette != 'balanced' else ''}"
                f"{' --dynamic' if a.dynamic else ''}{f' --red {a.red:g}' if a.red != 1.0 else ''}{f' --green {a.green:g}' if a.green != 1.0 else ''}{f' --blue {a.blue:g}' if a.blue != 1.0 else ''}"
                f" --sheet {a.sheet}. Do not edit.\n")
        if a.dynamic:
            f.write("# Dynamic brightness on. UNPLUG THE SOLENOID (see `help`). Level from paint:\n"
                    f"#   ./paint {a.out}.plot -ed=1.6 -raw\n")
        f.write(f"# {sheet[0]:g}x{sheet[1]:g} at origin {ox:g},{oy:g}; grid {W}x{H} at {pitch:g} mm; "
                f"{len(w.exits)} dead-end exits, {filled} filled; "
                f"{len(snakes)} snakes, {sum(m['found'] for _, m in snakes)} found an exit.\n\n")
        f.write("home\nstatus          # check WPos:393.000,0.000 before plotting\n\n")
        if not a.ink:
            L = a.wall_level
            # Walls at a steady, slow feed: at the light pen's full 25000 mm/min a
            # long stretch got ~5x less light per mm than a short piece, which
            # never gets up to speed (pieces <= 20 mm average ~2800 mm/min).
            f.write("lightpen on\n")
            if a.dynamic:
                f.write("speed 25000\ndynamic on\n")
            wc = a.wall_colour or f"{L} {L} {L}"
            if walls:
                f.write("\n# --- walls ---\n" + ("" if a.dynamic else f"speed {a.wall_speed}\n")
                        + f"colour {wc}\n")
        for pts, _ in walls:
            f.write(f"moveto {pts[0][0]:g} {pts[0][1]:g}\n")
            for p in pts[1:]:
                f.write(f"drawto {p[0]:g} {p[1]:g}\n")
        f.write("\n# --- snakes ---\n")
        if not a.ink and not a.dynamic:
            f.write("speed 25000     # back to the light pen's full feed\n")
        for pts, m in snakes:
            note = {'escape': ' (escaped from an undiscovered trap)', 'sweep': ' (last sweep)'}
            f.write(f"# snake{' (found an exit)' if m['found'] else note.get(m['role'], '')}\n")
            if not a.dots:
                if not a.ink:
                    f.write("colour %d %d %d\n" % m['colour'])
                f.write(f"moveto {pts[0][0]:g} {pts[0][1]:g}\n")
            else:
                # A dot of the inverse colour where the snake was born: a tiny
                # out-and-back along its first move. The pen all but stops at
                # each end, so the light piles up into a bright round spot.
                # The colour goes BEFORE the moveto, so plotter.pl preloads it
                # during the air move and the dot lights on time.
                (x0, y0), (x1, y1) = pts[0], pts[1]
                L = math.hypot(x1 - x0, y1 - y0) or 1.0
                dx, dy = a.dot * (x1 - x0) / L, a.dot * (y1 - y0) / L
                f.write("colour %d %d %d\n" % m['inverse'])
                f.write(f"moveto {x0:g} {y0:g}\n")
                f.write(f"drawto {x0 + dx:g} {y0 + dy:g}\n")
                f.write(f"drawto {x0:g} {y0:g}\n")
                f.write("colour %d %d %d\n" % m['colour'])
            for p in pts[1:]:
                f.write(f"drawto {p[0]:g} {p[1]:g}\n")
        for pts, _ in walls + snakes:
            mm += sum(abs(p[0] - q[0]) + abs(p[1] - q[1]) for p, q in zip(pts, pts[1:]))
        f.write("\n")
        if a.dynamic:
            f.write("dynamic off\n")
        f.write("penup\n")
        if not a.ink:
            f.write("colour off\n")
        f.write("home            # park at the home corner when done\n")

    # Preview, on black like a light-paint frame (white for ink).
    S = 4
    img = Image.new('RGB', (int(sheet[0] * S), int(sheet[1] * S)), (255, 255, 255) if a.ink else (0, 0, 0))
    d = ImageDraw.Draw(img)
    T = lambda p: ((p[0] - ox) * S, (sheet[1] - (p[1] - oy)) * S)
    lw = max(1, int(pitch * S * 0.55))
    for pts, _ in walls:
        g = min(255, a.wall_level * 3)       # preview: same ratio to the snakes as on the LED
        wcol = tuple(min(255, int(v) * 2) for v in a.wall_colour.split()) if a.wall_colour else (g, g, g)
        d.line([T(p) for p in pts], fill=(0, 0, 0) if a.ink else wcol, width=lw, joint='curve')
    vivid = lambda c: tuple(min(255, int(v * 255 / max(c))) for v in c)
    for pts, m in snakes:
        col = (0, 0, 0) if a.ink else vivid(m['colour'])
        d.line([T(p) for p in pts], fill=col, width=lw, joint='curve')
    if a.dots:                       # the birth dots, on top
        r = lw * 0.75
        for pts, m in snakes:
            x, y = T(pts[0])
            d.ellipse([x - r, y - r, x + r, y + r], fill=vivid(m['inverse']))
    img.save(OD.preview(f"{a.out}_preview.png"))

    roles = [m['role'] for _, m in snakes]
    print(f"grid {W}x{H} at {pitch:g} mm; traps {len(w.exits)}: {filled} found by snakes, "
          f"{escaped} with an escapee; snakes {len(snakes)} "
          f"({roles.count('seek')} seekers, {roles.count('escape')} escapees, "
          f"{roles.count('fill')} fill, {roles.count('sweep')} last-sweep); {mm / 1000:.1f} m of line")
    print(f"wrote {a.out}.plot and {a.out}_preview.png")


if __name__ == '__main__':
    main()
