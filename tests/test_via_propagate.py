#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Юнит-тесты чистых функций via_propagate.py (без OpenCV; нужен только numpy).

Запуск из корня репозитория:
    python tests/test_via_propagate.py
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import via_propagate as vp  # noqa: E402

FAILS = []


def ok(cond, name):
    print(('ok: ' if cond else 'FAIL: ') + name)
    if not cond:
        FAILS.append(name)


def fake_pack(nums, annotated=()):
    via = {'_via_settings': {'core': {'default_filepath': '/x'}}, '_via_img_metadata': {}}
    for n in nums:
        fn = f'garbage.{n:07d}.jpg'
        regs = [{'shape_attributes': {'name': 'rect', 'x': 10, 'y': 10, 'width': 5, 'height': 5},
                 'region_attributes': {'garbage': 'cubes'}}] * (1 if n in annotated else 0)
        via['_via_img_metadata'][fn + '100'] = {'filename': fn, 'size': 100, 'regions': regs}
    return via


def t_frame_number():
    ok(vp.frame_number('garbage.0002685.jpg') == 2685, 'frame_number')
    ok(vp.frame_number('noext') == -1, 'frame_number без номера')


def t_build_items():
    via = fake_pack([2690, 2685, 2688])
    items = vp.build_items(via)
    ok([d['num'] for d in items] == [2685, 2688, 2690], 'build_items сортирует по номеру')
    ok(vp.default_root(via) == '/x', 'default_root из JSON')


def t_parse_ranges():
    items = vp.build_items(fake_pack(list(range(2685, 2691)) + list(range(2692, 2698))))
    ok(vp.parse_ranges('2685-2690,2692-2697', items) == [(0, 5), (6, 11)], 'parse_ranges базовый')
    ok(vp.parse_ranges('2685-2692', items)[0] == (0, 6), 'parse_ranges: граница внутри разрыва 2691')
    for bad in ('2685-2690,2690-2697', 'мусор', '3000-3010'):
        try:
            vp.parse_ranges(bad, items)
            ok(False, f'parse_ranges должен отклонить "{bad}"')
        except ValueError:
            pass
    ok(True, 'parse_ranges отклоняет пересечения/мусор/пустое')


def t_warp_rect():
    Hr = np.array([[0, -1, 1000.0], [1, 0, 0.0], [0, 0, 1.0]])  # поворот 90° + перенос
    out = vp.warp_rect({'name': 'rect', 'x': 100, 'y': 200, 'width': 50, 'height': 80}, Hr, 2000, 2000)
    ok(out == (720, 100, 80, 50), 'warp_rect: поворот 90° точный')
    ok(vp.warp_rect({'name': 'rect', 'x': 5000, 'y': 5000, 'width': 10, 'height': 10},
                    np.eye(3), 2000, 2000) is None, 'warp_rect: полностью за кадром -> None')
    ok(vp.warp_rect({'name': 'rect', 'x': 1975, 'y': 100, 'width': 50, 'height': 40},
                    np.eye(3), 2000, 2000) == (1975, 100, 25, 40), 'warp_rect: клиппинг 50%')
    ok(vp.warp_rect({'name': 'rect', 'x': 1990, 'y': 100, 'width': 50, 'height': 40},
                    np.eye(3), 2000, 2000) is None, 'warp_rect: <25% в кадре -> None')


def t_unscale_H():
    Hs = np.array([[1.0, 0, 8.0], [0, 1.0, -4.0], [0, 0, 1.0]])
    H = vp.unscale_H(Hs, 0.5, 0.25)
    q = H @ np.array([1000.0, 600.0, 1.0])
    q_small = Hs @ np.array([500.0, 300.0, 1.0])
    ok(np.allclose(q[:2], q_small[:2] / 0.25), 'unscale_H с разными масштабами')


def t_sig_distance():
    a = {'thumb': np.random.default_rng(0).normal(0, 1, 100), 'hist': np.full(16, 1 / 16)}
    a['thumb'] = (a['thumb'] - a['thumb'].mean()) / a['thumb'].std()
    d0, _, _ = vp.sig_distance(a, a)
    b = {'thumb': -a['thumb'], 'hist': np.eye(16)[0] / 1.0}
    d1, _, _ = vp.sig_distance(a, b)
    ok(abs(d0) < 1e-9 and d1 > 0.5, 'sig_distance: 0 для себя, большое для чужого')


def t_suggest_donors():
    rng = np.random.default_rng(3)
    b1, b2 = rng.normal(0, 1, 100), rng.normal(5, 1, 100)
    h = np.full(16, 1 / 16)
    items = []
    for j in range(6):
        base = b1 if j < 3 else b2
        th = base + rng.normal(0, 0.01, 100)
        th = (th - th.mean()) / th.std()
        items.append({'sig': {'thumb': th, 'hist': h}, 'regions': []})
    donors = vp.suggest_donors(items, 0, 5, 2)
    ok(len(donors) == 2 and {d // 3 for d in donors} == {0, 1},
       f'suggest_donors покрывает обе подгруппы: {donors}')


def t_plan_roundtrip(tmp='tests/_plan_tmp.json'):
    items = vp.build_items(fake_pack(list(range(2685, 2697))))
    cl = [(0, 5), (6, 11)]
    vp.save_plan(tmp, 'p.json', cl, items, [[0, 2], [7]], {})
    plan, cl2 = vp.load_plan(tmp, items)
    os.remove(tmp)
    ok(cl2 == cl and plan['clusters'][0]['donors'] == ['garbage.0002685.jpg', 'garbage.0002687.jpg'],
       'план: сохранение/загрузка сохраняет границы и доноров')


def t_rect_regions():
    meta = {'regions': [
        {'shape_attributes': {'name': 'rect', 'x': 0, 'y': 0, 'width': 1, 'height': 1}},
        {'shape_attributes': {'name': 'polygon', 'all_points_x': [], 'all_points_y': []}}]}
    ok(len(vp.rect_regions(meta)) == 1, 'rect_regions отсекает не-rect')


def t_dedup_boxes():
    def A(donor, **kw):
        return dict({'x': 0, 'y': 0, 'w': 100, 'h': 100, 'donor': donor}, **kw)
    kept, dropped = vp.dedup_boxes([A(0), A(1)], 0.65)
    ok(len(kept) == 1 and kept[0]['donor'] == 0 and len(dropped) == 1,
       'dedup: идентичные боксы с разных доноров -> один (приоритет первого)')
    kept, _ = vp.dedup_boxes([A(0), A(1, x=40)], 0.65)
    ok(len(kept) == 2, 'dedup: перекрытие 60% < порога 0.65 -> оба остаются')
    kept, dropped = vp.dedup_boxes([A(0), A(1, x=20)], 0.65)
    ok(len(kept) == 1 and len(dropped) == 1, 'dedup: перекрытие 80% > порога -> дубликат выброшен')
    kept, _ = vp.dedup_boxes([A(0), A(0, x=10)], 0.65)
    ok(len(kept) == 2, 'dedup: перекрытие внутри одного донора сохраняется')
    big = A(0, w=200, h=200)
    small = A(1, x=50, y=50, w=20, h=20)
    kept, dropped = vp.dedup_boxes([big, small], 0.65)
    ok(len(kept) == 1 and kept[0]['w'] == 200,
       'dedup: кандидат, на 100% накрытый чужим боксом, выбрасывается')
    kept, _ = vp.dedup_boxes([small, big], 0.65)
    ok(len(kept) == 2, 'dedup: большой кандидат поверх маленького остаётся (правило асимметрично)')


if __name__ == '__main__':
    for f in [t_frame_number, t_build_items, t_parse_ranges, t_warp_rect, t_unscale_H,
              t_sig_distance, t_suggest_donors, t_plan_roundtrip, t_rect_regions, t_dedup_boxes]:
        f()
    print()
    if FAILS:
        print(f'{len(FAILS)} FAIL:', FAILS)
        sys.exit(1)
    print('Все тесты via_propagate пройдены')
