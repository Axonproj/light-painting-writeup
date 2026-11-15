"""outdirs.py — where the generators put what they write.

A bare file name (no folder in it) goes into the project's folder for its
kind; a name with a folder is left alone, so `--out wave_shots/x` still works.

    plots/      .plot files            OD.plot('rings3d.plot')
    previews/   previews, references,  OD.preview('rings3d_preview.png')
                trace checks
    svg/        SVGs                   OD.svg('keep_me_safe_edit.svg')
    docs/       PDFs to print          OD.doc('sharpness_card.pdf')

The result is relative to the current folder when it is inside the project,
so printed paths stay short.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _put(path, sub):
    if os.path.dirname(path):
        return path
    d = os.path.join(ROOT, sub)
    os.makedirs(d, exist_ok=True)
    full = os.path.join(d, path)
    rel = os.path.relpath(full)
    return full if rel.startswith('..') else rel


def plot(path):    return _put(path, 'plots')
def preview(path): return _put(path, 'previews')
def svg(path):     return _put(path, 'svg')
def doc(path):     return _put(path, 'docs')
