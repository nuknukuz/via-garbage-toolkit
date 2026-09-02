#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты lens-компенсации дисторсии (via_propagate v4.3).

Чистый numpy, cv2 НЕ нужен. Запуск:  python tests/test_lens_k.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import numpy as np

import via_propagate as vp

FAILS = []


def ok(cond, msg):
    print(('  OK  ' if cond else ' FAIL ') + msg)
    if not cond:
        FAILS.append(msg)


def t_lens_roundtrip():
    rng = np.random.default_rng(0)
    u = rng.uniform(-1, 1, (500, 2)) * 0.95
    for k in (-0.02, -0.05, -0.10, -0.15):
        e1 = np.abs(vp.lens_distort(vp.lens_undistort(u, k), k) - u).max()
        e2 = np.abs(vp.lens_undistort(vp.lens_distort(u, k), k) - u).max()
        ok(e1 < 1e-9 and e2 < 1e-9, f'lens: distort/undistort взаимно обратны, k={k}')


def t_weighted_median():
    ok(vp.weighted_median([1, 2, 3, 4], [1, 1, 1, 1]) == 2, 'wmedian: равные веса')
    ok(vp.weighted_median([1, 2, 3, 4], [10, 1, 1, 1]) == 1, 'wmedian: веса тянут')
    ok(vp.weighted_median([5.0], [3.0]) == 5.0, 'wmedian: один элемент')


def t_lm_recovers_k():
    rng = np.random.default_rng(1)
    k_true = -0.08
    H_true = np.array([[0.999, 0.01, 0.02],
                       [-0.008, 1.001, -0.015],
                       [1e-5, -2e-5, 1.0]])
    us = rng.uniform(-0.7, 0.7, (200, 2)) * np.array([1.0, 0.75])
    ut = vp.lens_chain(us, H_true, k_true)
    H0 = H_true + rng.normal(0, 1e-3, (3, 3))
    H0[2, 2] = 1.0
    Hn, k_hat, converged = vp.lm_refine_Hk(us, ut, H0, k0=0.0)
    ok(converged and abs(k_hat - k_true) < 1e-4,
       f'ЛМ: свободный k восстановлен ({k_hat:+.5f} vs {k_true})')
    ok(np.abs(Hn - H_true).max() < 1e-4, 'ЛМ: H восстановлена')
    Hn2, k2, ok2 = vp.lm_refine_Hk(us, ut, H0, k_fix=-0.05)
    ok(ok2 and k2 == -0.05, 'ЛМ: k_fix уважается')
    Hn3, k3, ok3 = vp.lm_refine_Hk(us, ut, H0, k_fix=k_true)
    res = np.linalg.norm(vp.lens_chain(us, Hn3, k3) - ut)
    ok(ok3 and res < 1e-6, 'ЛМ: при точном k остаток ~ 0')


def t_warp_box_lens():
    k = -0.08
    W, H = 4000, 3000
    Hn = np.array([[0.999, 0.01, 0.02],
                   [-0.008, 1.001, -0.015],
                   [1e-5, -2e-5, 1.0]])
    model = {'Hn': Hn, 'k': k, 'wA': W, 'hA': H, 'wB': W, 'hB': H}
    sa = {'x': 3300.0, 'y': 2400.0, 'width': 140.0, 'height': 100.0}
    wh = vp.warp_box_lens(sa, model)
    ok(wh is not None, 'warp_box_lens: бокс перенесён')
    pts = np.array([[3300, 2400], [3440, 2400], [3440, 2500], [3300, 2500]], float)
    (cx, cy), f0 = vp.lens_norm_params(W, H)
    q = vp.lens_chain((pts - np.array([cx, cy])) / f0, Hn, k) * f0 + np.array([cx, cy])
    x0, y0 = q[:, 0].min(), q[:, 1].min()
    ok(abs(wh[0] - round(x0)) <= 1 and abs(wh[1] - round(y0)) <= 1,
       'warp_box_lens: bbox совпадает с цепочкой D^-1->H->D')
    ok(abs(wh[2] - (q[:, 0].max() - x0)) <= 1.5 and abs(wh[3] - (q[:, 1].max() - y0)) <= 1.5,
       'warp_box_lens: размеры совпадают')
    far = {'x': -5000.0, 'y': -5000.0, 'width': 100.0, 'height': 100.0}
    ok(vp.warp_box_lens(far, model) is None, 'warp_box_lens: бокс вне кадра -> None')


if __name__ == '__main__':
    for f in [t_lens_roundtrip, t_weighted_median, t_lm_recovers_k, t_warp_box_lens]:
        f()
    print()
    if FAILS:
        print('ПРОВАЛЫ:', *FAILS, sep='\n - ')
        sys.exit(1)
    print('все тесты прошли')
