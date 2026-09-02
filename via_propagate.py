#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
via_propagate.py v4.3 — полуавтоматическая разметка паков кадров VIA 2.x.

РАБОЧИЙ ЦИКЛ (два запуска):

  1) Планирование (интерактивно):
       python via_propagate.py pack_014.json --plan plan14.json --root .
     - строит словарь признаков (эскиз + HSV-гистограмма + ORB-кэш на кадр);
     - режет пак на кластеры (разрыв нумерации ИЛИ мало совпадений дескрипторов
       ИЛИ скачок сигнатурного расстояния — все три критерия сразу);
     - показывает разбиение и спрашивает, норм ли оно; при ответе "n" просит
       границы вручную в формате  2685-2730,2731-2912;
     - внутри каждого кластера предлагает 2-5 кадров под ручную разметку
       (жадное max-min покрытие по сигнатурам + проверка покрытия дескрипторами);
     - сохраняет план в plan14.json и список кадров в plan14_donors.txt.

  2) Разметка предложенных кадров-доноров в VIA (Project -> Save).

  3) Применение:
       python via_propagate.py pack_014_saved.json --use-plan plan14.json --root . --out filled.json --draw previews14
     - ЦЕЛИ = ВСЕ кадры кластеров, КРОМЕ доноров из плана (старая разметка
       на целях заменяется перенесённой; входной json не меняется);
     - по умолчанию донор для цели один — по максимуму совпадений дескрипторов;
     - с --merge-donors на цель переносится СУММА боксов со всех размеченных
       доноров кластера: каждый донор матчится отдельно (гомография по кэшу),
       затем дубликаты схлопываются: если >= --dup-overlap (0.65) площади бокса
       перекрыто уже оставленным боксом ДРУГОГО донора — бокс выбрасывается.
       Приоритет у донора с большим числом совпадений с целью. Перекрытия
       внутри одного донора не трогаем (там соседние объекты — норма);
     - доноры (из плана или --donors) могут лежать ВНЕ интервалов кластеров —
       тогда они используются как источники для всех целей (в одиночном режиме
       дополнительно режутся --max-dist);
     - доноры, не прошедшие матчинг с целью, видны в отчёте: строка цели
       заканчивается на "(не сошлись: файл:статус)" + колонка failed_donors.
     - с --lens-k auto учитывается радиальная дисторсия объектива
       (division model, 1 параметр): k оценивается по совпадениям пар внутри
       КАЖДОГО кластера (объектив константен внутри кластера, между
       кластерами может меняться), затем H уточняется ЛМ при
       фиксированном k кластера; --lens-k ЧИСЛО — фиксированный k.

  Резерв (если план потерян/терминал слетел):
       python via_propagate.py marked.json --clusters "2685-2730,2731-2912" --donors "garbage.0002690.jpg,garbage.0002738.jpg" --root . --out filled.json [--merge-donors]

  Режимы целей (--targets-mode):
     auto      — nondonors, если задан план/--donors, иначе empty (по умолчанию)
     nondonors — все кадры кластеров, кроме доноров (старая разметка заменяется!)
     empty     — только кадры без единого бокса

Зависимости:  pip install opencv-python-headless numpy
"""
import argparse
import copy
import csv
import json
import os
import re
import sys


# ----------------------------- утилиты VIA JSON -----------------------------

def load_via(path):
    with open(path, encoding='utf-8') as f:
        via = json.load(f)
    for k in ('_via_img_metadata', '_via_settings'):
        if k not in via:
            raise ValueError(f'{path}: нет ключа {k} — это точно проект VIA 2.x?')
    return via


def frame_number(filename):
    m = re.search(r'(\d+)(?=\.[a-zA-Z]+$)', filename)
    return int(m.group(1)) if m else -1


def default_root(via):
    return (via.get('_via_settings', {}).get('core', {}) or {}).get('default_filepath', '') or ''


def build_items(via):
    items = []
    for key, meta in via['_via_img_metadata'].items():
        items.append({'key': key, 'fn': meta.get('filename', ''),
                      'num': frame_number(meta.get('filename', '')),
                      'regions': meta.get('regions', [])})
    items.sort(key=lambda d: (d['num'], d['fn']))
    return items


def rect_regions(meta):
    return [r for r in meta['regions']
            if r.get('shape_attributes', {}).get('name') == 'rect']


# ----------------------------- геометрия -----------------------------

def unscale_H(Hs, s1, s2):
    import numpy as np
    S1 = np.diag([s1, s1, 1.0])
    S2inv = np.diag([1.0 / s2, 1.0 / s2, 1.0])
    H = S2inv @ Hs @ S1
    return H / H[2, 2]


def warp_rect(sa, H, img_w, img_h, min_keep_frac=0.25, min_side=3):
    import numpy as np
    x, y, w, h = sa['x'], sa['y'], sa['width'], sa['height']
    pts = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float64)
    pts = np.hstack([pts, np.ones((4, 1))])
    q = (H @ pts.T).T
    q = q[:, :2] / q[:, 2:3]
    x0 = int(round(max(0, q[:, 0].min()))); x1 = int(round(min(img_w, q[:, 0].max())))
    y0 = int(round(max(0, q[:, 1].min()))); y1 = int(round(min(img_h, q[:, 1].max())))
    area_warped = max(0.0, q[:, 0].max() - q[:, 0].min()) * max(0.0, q[:, 1].max() - q[:, 1].min())
    nw, nh = x1 - x0, y1 - y0
    if nw < min_side or nh < min_side:
        return None
    if area_warped > 0 and (nw * nh) / area_warped < min_keep_frac:
        return None
    return x0, y0, nw, nh


def overlap_frac(a, b):
    """Доля площади бокса a, перекрытая боксом b. Боксы: {'x','y','w','h'}."""
    x0 = max(a['x'], b['x']); y0 = max(a['y'], b['y'])
    x1 = min(a['x'] + a['w'], b['x'] + b['w'])
    y1 = min(a['y'] + a['h'], b['y'] + b['h'])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    return inter / max(1, a['w'] * a['h'])


def dedup_boxes(boxes, thr=0.65):
    """Дедупликация суммы боксов, перенесённых с РАЗНЫХ доноров.

    boxes — в порядке приоритета (раньше = надёжнее). Бокс выбрасывается,
    если >= thr его площади перекрыто уже оставленным боксом другого донора.
    Перекрытия внутри одного донора сохраняются (соседние объекты — норма).
    Возвращает (kept, dropped)."""
    kept, dropped = [], []
    for b in boxes:
        if any(k['donor'] != b['donor'] and overlap_frac(b, k) >= thr for k in kept):
            dropped.append(b)
        else:
            kept.append(b)
    return kept, dropped


# ----------------------------- словарь признаков -----------------------------

def sig_distance(sa, sb):
    """(1 - corr эскизов + Bhattacharyya гистограмм)/2. Чистый numpy."""
    import numpy as np
    d_thumb = 1.0 - float(np.mean(sa['thumb'] * sb['thumb']))
    bc = float(np.sqrt(sa['hist'] * sb['hist']).sum())
    d_hist = float(np.sqrt(max(0.0, 1.0 - bc)))
    return (d_thumb + d_hist) / 2.0, d_thumb, d_hist


def build_dictionary(items, root, max_dim=1600, nfeatures=2500, verbose=True):
    """Один проход по кадрам: сигнатура + ORB-кэш в items[i]."""
    try:
        import cv2
    except ImportError:
        sys.exit('Нужен OpenCV:  pip install opencv-python-headless')
    import numpy as np
    orb = cv2.ORB_create(nfeatures=nfeatures, fastThreshold=12)
    n = len(items)
    for i, it in enumerate(items):
        img = cv2.imread(os.path.join(root, it['fn']))
        if img is None:
            it.update(sig=None, kp=[], des=None, s=1.0)
            continue
        it['h'], it['w'] = img.shape[:2]
        s = min(1.0, max_dim / max(it['w'], it['h']))
        it['s'] = s
        small = cv2.resize(img, None, fx=s, fy=s) if s < 1.0 else img
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        thumb = cv2.resize(gray, (48, 27), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()
        thumb = (thumb - thumb.mean()) / (thumb.std() + 1e-6)
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [18, 8], [0, 180, 0, 256]).astype(np.float32)
        hist = (hist / max(hist.sum(), 1e-9)).ravel()
        kp, des = orb.detectAndCompute(gray, None)
        it.update(sig={'thumb': thumb, 'hist': hist}, kp=kp or [], des=des)
        if verbose and (i + 1) % 25 == 0:
            print(f'  ... сигнатуры: {i + 1}/{n}', flush=True)


def good_matches(des1, des2, ratio=0.78):
    import cv2
    if des1 is None or des2 is None or len(des1) < 8 or len(des2) < 8:
        return []
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    knn = bf.knnMatch(des1, des2, k=2)
    return [m for m, nn in (p for p in knn if len(p) == 2) if m.distance < ratio * nn.distance]


def estimate_H_cached(A, B, ransac_thr=3.0, min_inliers=12,
                      min_inlier_ratio=0.25, allow_affine=True, ratio=0.78):
    """Гомография A->B по кэшированным дескрипторам."""
    try:
        import cv2
    except ImportError:
        sys.exit('Нужен OpenCV:  pip install opencv-python-headless')
    import numpy as np
    good = good_matches(A.get('des'), B.get('des'), ratio)
    stats = {'matches': len(good), 'img_w': B.get('w'), 'img_h': B.get('h')}
    if len(good) < 8:
        stats['status'] = 'few_matches'
        return None, stats
    p1 = np.float32([A['kp'][m.queryIdx].pt for m in good])
    p2 = np.float32([B['kp'][m.trainIdx].pt for m in good])
    flags = getattr(cv2, 'USAC_MAGSAC', cv2.RANSAC)
    Hs, inl = cv2.findHomography(p1, p2, flags, ransac_thr)
    method = 'homography'
    if Hs is None and allow_affine:
        M, inl = cv2.estimateAffinePartial2D(p1, p2, method=cv2.RANSAC,
                                             ransacReprojThreshold=ransac_thr)
        if M is not None:
            Hs = np.vstack([M, [0.0, 0.0, 1.0]])
            method = 'affine_partial'
    if Hs is None:
        stats['status'] = 'no_homography'
        return None, stats
    n_inl = int(inl.sum()) if inl is not None else 0
    stats.update({'inliers': n_inl, 'inlier_ratio': round(n_inl / max(1, len(good)), 3),
                  'method': method})
    if n_inl < min_inliers or stats['inlier_ratio'] < min_inlier_ratio:
        stats['status'] = 'low_inliers'
        return None, stats
    H = unscale_H(Hs.astype(np.float64), A.get('s', 1.0), B.get('s', 1.0))
    stats['status'] = 'ok'
    return H, stats


# ----------------------------- кластеры -----------------------------

def cut_clusters(items, min_edge_matches=25, cut_on_sig=True, cut_k=4.0, cut_abs=0.55):
    """Ребро плохое, если: разрыв нумерации; мало совпадений дескрипторов;
    (cut_on_sig) скачок сигнатурного расстояния > med + k*MAD или > cut_abs."""
    import numpy as np
    n = len(items)
    edges = []
    for k in range(n - 1):
        a, b = items[k], items[k + 1]
        gap = (b['num'] - a['num']) > 1
        gm = 0 if gap else len(good_matches(a.get('des'), b.get('des')))
        d = dt = dh = None
        if a.get('sig') is not None and b.get('sig') is not None:
            d, dt, dh = sig_distance(a['sig'], b['sig'])
        edges.append({'a': k, 'b': k + 1, 'gap': gap, 'good_matches': gm,
                      'd_sig': d, 'd_thumb': dt, 'd_hist': dh})
    ds = [e['d_sig'] for e in edges if not e['gap'] and e['d_sig'] is not None]
    T = None
    if cut_on_sig and len(ds) >= 8:
        med = float(np.median(ds))
        mad = float(np.median([abs(x - med) for x in ds])) * 1.4826
        T = med + cut_k * mad
    for e in edges:
        cut = e['gap'] or e['good_matches'] < min_edge_matches
        if cut_on_sig and e['d_sig'] is not None and not e['gap']:
            cut = cut or (T is not None and e['d_sig'] > T) or e['d_sig'] > cut_abs
        e['cut'] = cut
    clusters, start = [], 0
    for k, e in enumerate(edges):
        if e['cut']:
            clusters.append((start, k))
            start = k + 1
    clusters.append((start, n - 1))
    return edges, clusters, T


def parse_ranges(spec, items):
    """'2685-2730, 2731-2912' -> [(lo_idx, hi_idx)]. Терпимо к разрывам нумерации."""
    clusters = []
    for part in spec.split(','):
        part = part.strip()
        m = re.fullmatch(r'(\d+)\s*-\s*(\d+)', part)
        if not m:
            raise ValueError(f'не понял фрагмент "{part}" (нужно ЧИСЛО-ЧИСЛО)')
        lo_n, hi_n = sorted((int(m.group(1)), int(m.group(2))))
        lo = min((i for i, it in enumerate(items) if it['num'] >= lo_n), default=None)
        hi = max((i for i, it in enumerate(items) if it['num'] <= hi_n), default=None)
        if lo is None or hi is None or lo > hi:
            raise ValueError(f'диапазон {part} не накрывает ни одного кадра')
        clusters.append((lo, hi))
    clusters.sort()
    for (a, b), (c, d) in zip(clusters, clusters[1:]):
        if c <= b:
            raise ValueError('диапазоны пересекаются')
    return clusters


def cluster_of(clusters, i):
    for lo, hi in clusters:
        if lo <= i <= hi:
            return (lo, hi)
    return None


# ----------------------------- выбор доноров -----------------------------

def suggest_donors(items, lo, hi, k):
    """k кадров под разметку: медоид + жадное max-min покрытие по сигнатурам."""
    import numpy as np
    valid = [i for i in range(lo, hi + 1) if items[i].get('sig') is not None]
    if len(valid) <= k:
        return sorted(valid)
    m = len(valid)
    D = np.zeros((m, m))
    for a in range(m):
        for b in range(a + 1, m):
            D[a, b] = D[b, a] = sig_distance(items[valid[a]]['sig'], items[valid[b]]['sig'])[0]
    pos = {idx: p for p, idx in enumerate(valid)}
    donors = [valid[int(np.argmin(D.sum(axis=1)))]]  # медоид
    while len(donors) < k:
        cand = [i for i in valid if i not in donors]
        nxt = max(cand, key=lambda i: min(D[pos[i], pos[d]] for d in donors))
        donors.append(nxt)
    return sorted(donors)


def coverage_check(items, lo, hi, donors, min_matches):
    """Кадры кластера, плохо покрытые донорами (< min_matches совпадений со всеми)."""
    poor = []
    for i in range(lo, hi + 1):
        if i in donors or items[i].get('des') is None:
            continue
        best = max((len(good_matches(items[i]['des'], items[d]['des'])) for d in donors),
                   default=0)
        if best < min_matches:
            poor.append(i)
    return poor


def donor_pool(lo, hi, donor_idx=None):
    """Пул кандидатов в доноры: кадры кластера ∪ явно заданные доноры
    (те могут лежать вне интервалов кластеров — удобно для ручного режима)."""
    pool = set(range(lo, hi + 1))
    if donor_idx:
        pool |= set(donor_idx)
    return sorted(pool)


def pick_donor(items, ti, lo, hi, min_regions, max_dist, ratio=0.78, allowed=None,
               donor_idx=None):
    """Донор для цели: максимум совпадений дескрипторов, при равенстве — ближайший."""
    best = None
    for si in donor_pool(lo, hi, donor_idx):
        if si == ti or abs(si - ti) > max_dist:
            continue
        if allowed is not None and si not in allowed:
            continue
        if len(rect_regions(items[si])) < min_regions:
            continue
        gm = len(good_matches(items[ti].get('des'), items[si].get('des'), ratio))
        score = (gm, -abs(si - ti))
        if best is None or score > best[0]:
            best = (score, si)
    return (best[1], best[0][0]) if best else (None, 0)


# ----------------------------- дисторсия объектива -----------------------------

def lens_norm_params(w, h):
    """Нормализация координат: центр кадра — главная точка, полудиагональ —
    масштаб (угол кадра имеет r = 1)."""
    return (w / 2.0, h / 2.0), 0.5 * (w * w + h * h) ** 0.5


def lens_undistort(u, k):
    """Division model (1 параметр): искажённая -> идеальная точка. k<0 — бочка."""
    import numpy as np
    r2 = (u ** 2).sum(axis=-1, keepdims=True)
    return u / (1.0 + k * r2)


def lens_distort(u, k):
    """Division model: идеальная -> искажённая (замкнутая форма, обратная к lens_undistort)."""
    import numpy as np
    r = np.sqrt((u ** 2).sum(axis=-1, keepdims=True))
    return u * (2.0 / (1.0 + np.sqrt(np.maximum(1e-12, 1.0 - 4.0 * k * r ** 2))))


def _apply_Hn(H, u):
    import numpy as np
    q = (H @ np.hstack([u, np.ones((len(u), 1))]).T).T
    return q[:, :2] / q[:, 2:3]


def lens_chain(u_s, Hn, k):
    """Строгая карта переноса в норм. координатах: D^-1 -> H -> D."""
    return lens_distort(_apply_Hn(Hn, lens_undistort(u_s, k)), k)


def weighted_median(values, weights):
    import numpy as np
    v = np.asarray(values, float)
    w = np.asarray(weights, float)
    o = np.argsort(v)
    v, w = v[o], w[o]
    cw = np.cumsum(w)
    return float(v[min(len(v) - 1, int(np.searchsorted(cw, 0.5 * cw[-1])))])


def lm_refine_Hk(us, ut, H0, k0=0.0, k_fix=None, max_iter=80):
    """Левенберг–Марквардт по (H, k) на ИНЛАЕРАХ (L2; робастность уже дал MAGSAC).
    us, ut — Nx2 норм. координаты искажённых точек; H0 — старт в норм. координатах.
    k_fix=None -> k свободен; иначе H уточняется при фиксированном k_fix.
    Без scipy: численный якобиан (8-9 параметров). Возвращает (Hn, k, ok)."""
    import numpy as np
    free_k = k_fix is None

    def unpack(th):
        H = np.array([[th[0], th[1], th[2]],
                      [th[3], th[4], th[5]],
                      [th[6], th[7], 1.0]])
        return H, (th[8] if free_k else k_fix)

    th = np.concatenate([H0[:2].ravel(), H0[2, :2], [k0 if free_k else 0.0]])
    th = th[:9] if free_k else th[:8]

    def res(th_):
        H, k = unpack(th_)
        return (lens_chain(us, H, k) - ut).ravel()

    r = res(th)
    cost = float((r ** 2).sum())
    lam, ok = 1e-3, False
    for _ in range(max_iter):
        J = np.empty((len(r), len(th)))
        for j in range(len(th)):
            stp = 1e-6 * max(1.0, abs(th[j]))
            th2 = th.copy()
            th2[j] += stp
            J[:, j] = (res(th2) - r) / stp
        A = J.T @ J
        g = J.T @ r
        try:
            d = np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-12), -g)
        except np.linalg.LinAlgError:
            break
        th_new = th + d
        Hn, kn = unpack(th_new)
        bad = (not np.isfinite(Hn).all()) or (free_k and (not np.isfinite(kn) or abs(kn) > 0.35))
        r_new = None if bad else res(th_new)
        if not bad and float((r_new ** 2).sum()) < cost:
            th, r, cost = th_new, r_new, float((r_new ** 2).sum())
            lam = max(lam * 0.3, 1e-9)
            ok = True
            if np.linalg.norm(d) < 1e-12:
                break
        else:
            lam *= 10
            if lam > 1e8:
                break
    H, k = unpack(th)
    return H, k, ok


def estimate_Hk_cached(A, B, good, ransac_thr=3.0, min_inliers=12,
                       min_inlier_ratio=0.25, allow_affine=True, k_fix=None):
    """Перенос с учётом дисторсии. MAGSAC по искажённым точкам (как раньше) ->
    ЛМ-уточнение (H, k) на инлаерах -> финальные инлаеры по репроекции через
    lens_chain -> те же гейты min_inliers / min_inlier_ratio.
    Возвращает (model | None, stats); model = {'Hn', 'k', 'wA', 'hA', 'wB', 'hB'}."""
    try:
        import cv2
    except ImportError:
        sys.exit('Нужен OpenCV:  pip install opencv-python-headless')
    import numpy as np

    stats = {'matches': len(good), 'img_w': B.get('w'), 'img_h': B.get('h')}
    wA, hA, wB, hB = A.get('w'), A.get('h'), B.get('w'), B.get('h')
    if len(good) < 8 or not all((wA, hA, wB, hB)):
        stats['status'] = 'few_matches'
        return None, stats
    p1 = np.float32([A['kp'][m.queryIdx].pt for m in good])
    p2 = np.float32([B['kp'][m.trainIdx].pt for m in good])
    flags = getattr(cv2, 'USAC_MAGSAC', cv2.RANSAC)
    Hs, inl = cv2.findHomography(p1, p2, flags, ransac_thr)
    method = 'homography'
    if Hs is None and allow_affine:
        M, inl = cv2.estimateAffinePartial2D(p1, p2, method=cv2.RANSAC,
                                             ransacReprojThreshold=ransac_thr)
        if M is not None:
            Hs = np.vstack([M, [0.0, 0.0, 1.0]])
            method = 'affine_partial'
    if Hs is None:
        stats['status'] = 'no_homography'
        return None, stats

    sA, sB = A.get('s', 1.0), B.get('s', 1.0)
    H_pix = unscale_H(Hs.astype(np.float64), sA, sB)
    (cxA, cyA), fA = lens_norm_params(wA, hA)
    (cxB, cyB), fB = lens_norm_params(wB, hB)
    Ns = np.array([[1 / fA, 0, -cxA / fA], [0, 1 / fA, -cyA / fA], [0, 0, 1.0]])
    Nt = np.array([[1 / fB, 0, -cxB / fB], [0, 1 / fB, -cyB / fB], [0, 0, 1.0]])
    Hn0 = Nt @ H_pix @ np.linalg.inv(Ns)
    Hn0 = Hn0 / Hn0[2, 2]
    us = (p1.astype(np.float64) / sA - np.array([cxA, cyA])) / fA
    ut = (p2.astype(np.float64) / sB - np.array([cxB, cyB])) / fB

    inl_mask = inl.ravel().astype(bool) if inl is not None else np.ones(len(good), bool)
    model, k_est = None, 0.0
    if int(inl_mask.sum()) >= 6:
        Hn, k_try, ok_lm = lm_refine_Hk(us[inl_mask], ut[inl_mask], Hn0,
                                        k0=0.0, k_fix=k_fix)
        if ok_lm and np.isfinite(Hn).all():
            model = {'Hn': Hn, 'k': float(k_try), 'wA': wA, 'hA': hA, 'wB': wB, 'hB': hB}
            k_est = float(k_try)
            method += '+lens'
    if model is None:  # ЛМ не сошёлся — честная гомография без дисторсии (k=0)
        model = {'Hn': Hn0, 'k': 0.0, 'wA': wA, 'hA': hA, 'wB': wB, 'hB': hB}

    err = np.linalg.norm(lens_chain(us, model['Hn'], model['k']) - ut, axis=1) * fB
    inl2 = err < ransac_thr
    n_inl = int(inl2.sum())
    stats.update({'inliers': n_inl,
                  'inlier_ratio': round(n_inl / max(1, len(good)), 3),
                  'method': method, 'k': round(k_est, 4)})
    if n_inl < min_inliers or stats['inlier_ratio'] < min_inlier_ratio:
        stats['status'] = 'low_inliers'
        return None, stats
    stats['status'] = 'ok'
    return model, stats


def warp_box_lens(sa, model, min_keep_frac=0.25, min_side=3):
    """Перенос rect-бокса цепочкой D^-1 -> H -> D: углы -> норм. -> идеал -> H ->
    искажение -> пиксели цели -> axis-aligned bbox. Гейты как в warp_rect."""
    import numpy as np
    x, y, w, h = sa['x'], sa['y'], sa['width'], sa['height']
    pts = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float64)
    (cxA, cyA), fA = lens_norm_params(model['wA'], model['hA'])
    (cxB, cyB), fB = lens_norm_params(model['wB'], model['hB'])
    u = (pts - np.array([cxA, cyA])) / fA
    q = lens_chain(u, model['Hn'], model['k']) * fB + np.array([cxB, cyB])
    x0 = int(round(max(0, q[:, 0].min()))); x1 = int(round(min(model['wB'], q[:, 0].max())))
    y0 = int(round(max(0, q[:, 1].min()))); y1 = int(round(min(model['hB'], q[:, 1].max())))
    area_warped = max(0.0, q[:, 0].max() - q[:, 0].min()) * max(0.0, q[:, 1].max() - q[:, 1].min())
    nw, nh = x1 - x0, y1 - y0
    if nw < min_side or nh < min_side:
        return None
    if area_warped > 0 and (nw * nh) / area_warped < min_keep_frac:
        return None
    return x0, y0, nw, nh


def _lens_prepass(items, targets, clusters, manual, donor_idx, merge_donors,
                  min_source_regions, max_dist, min_inliers, min_inlier_ratio,
                  allow_affine, lens):
    """Пред-проход по кластерам: парные оценки (H, k) -> k кластера как
    взвешенная медиана парных (объектив константен ВНУТРИ кластера) -> рефит H
    при фиксированном k кластера. Возвращает (pair_models, pair_stats, donor_choice)."""
    import numpy as np
    pair_models, pair_stats, donor_choice = {}, {}, {}
    pairs_by_cluster = {}
    mcache = {}

    def good_for(si, ti):
        key = (si, ti)
        if key not in mcache:
            mcache[key] = good_matches(items[ti].get('des'), items[si].get('des'))
        return mcache[key]

    def estimate(si, ti, k_fix):
        model, stats = estimate_Hk_cached(items[si], items[ti], good_for(si, ti),
                                          min_inliers=min_inliers,
                                          min_inlier_ratio=min_inlier_ratio,
                                          allow_affine=allow_affine, k_fix=k_fix)
        pair_models[(si, ti)] = model
        pair_stats[(si, ti)] = stats

    for ti in targets:
        cl = cluster_of(clusters, ti) or (ti, ti)
        lo, hi = cl
        if merge_donors:
            donors = [si for si in donor_pool(lo, hi, donor_idx)
                      if si != ti and si in manual
                      and len(rect_regions(items[si])) >= min_source_regions
                      and len(good_for(si, ti)) > 0]
        else:
            si, _ = pick_donor(items, ti, lo, hi, min_source_regions, max_dist,
                               allowed=manual, donor_idx=donor_idx)
            donor_choice[ti] = si
            donors = [si] if si is not None else []
        for si in donors:
            if (si, ti) not in pair_models:
                estimate(si, ti, None if lens == 'auto' else lens)
            pairs_by_cluster.setdefault(cl, set()).add((si, ti))

    if lens == 'auto':
        for cl, plist in sorted(pairs_by_cluster.items()):
            ok = [(pair_stats[p]['k'], max(1, pair_stats[p].get('inliers', 0)))
                  for p in plist if pair_stats[p].get('status') == 'ok'
                  and pair_stats[p].get('method', '').endswith('+lens')]
            if not ok:
                continue
            vals = [a for a, _ in ok]
            wts = [b for _, b in ok]
            k_cl = (weighted_median(vals, wts) if len(vals) >= 3
                    else float(np.average(vals, weights=wts)))
            for si, ti in plist:
                estimate(si, ti, k_cl)
            print(f'  lens k [кадры {items[cl[0]]["num"]}–{items[cl[1]]["num"]}]: '
                  f'{k_cl:+.4f} (пар: {len(plist)}, сошлось: {len(ok)}, '
                  f'разброс ±{float(np.std(vals)):.4f})', flush=True)
    return pair_models, pair_stats, donor_choice


# ----------------------------- перенос -----------------------------

def propagate(via, root, targets, max_dist=8, min_source_regions=3,
              min_inliers=12, min_inlier_ratio=0.25, allow_affine=True,
              chain=False, clusters=None, items=None,
              merge_donors=False, dup_overlap=0.65, donor_idx=None,
              lens='off'):
    try:
        import cv2  # noqa: F401
    except ImportError:
        sys.exit('Нужен OpenCV:  pip install opencv-python-headless')

    if items is None:
        items = build_items(via)
    manual = set(range(len(items))) - set(targets)
    rows = []

    pair_models = pair_stats = None
    donor_choice = {}
    if lens != 'off':
        if clusters is None:
            clusters = [(0, len(items) - 1)]
        pair_models, pair_stats, donor_choice = _lens_prepass(
            items, targets, clusters, manual, donor_idx, merge_donors,
            min_source_regions, max_dist, min_inliers, min_inlier_ratio,
            allow_affine, lens)

    for ti in targets:
        t = items[ti]
        if clusters is not None:
            cl = cluster_of(clusters, ti)
            lo, hi = cl if cl else (ti, ti)
            allowed = None if chain else manual

            if merge_donors:
                if lens != 'off':
                    rows.append(_merge_donors_row_lens(
                        via, items, ti, lo, hi, allowed, min_source_regions,
                        dup_overlap, donor_idx, pair_models, pair_stats))
                else:
                    rows.append(_merge_donors_row(
                        via, items, ti, lo, hi, allowed, min_source_regions,
                        min_inliers, min_inlier_ratio, allow_affine, dup_overlap,
                        donor_idx=donor_idx))
                if chain:
                    manual.add(ti)
                continue

            if lens != 'off':
                si = donor_choice.get(ti)
                donor_score = pair_stats.get((si, ti), {}).get('matches', 0) \
                    if si is not None else 0
            else:
                si, donor_score = pick_donor(items, ti, lo, hi, min_source_regions,
                                             max_dist, allowed=allowed, donor_idx=donor_idx)
            if si is None:
                rows.append({'target': t['fn'], 'status': 'no_source_in_cluster',
                             'dist': '', 'source': '', 'boxes_in': 0, 'boxes_kept': 0})
                continue
            dist = abs(si - ti)
            src = items[si]
            if lens != 'off':
                model = pair_models.get((si, ti))
                stats = dict(pair_stats.get((si, ti), {}))
                warp_fn = (lambda sa: warp_box_lens(sa, model)) if model else None
            else:
                H, stats = estimate_H_cached(src, t, min_inliers=min_inliers,
                                             min_inlier_ratio=min_inlier_ratio,
                                             allow_affine=allow_affine)
                warp_fn = (lambda sa: warp_rect(sa, H, stats['img_w'], stats['img_h'])) \
                    if H is not None else None
            stats['donor_matches'] = donor_score
            if warp_fn is None:
                rows.append({'target': t['fn'], 'source': src['fn'], 'dist': dist,
                             **stats, 'boxes_in': 0, 'boxes_kept': 0})
                continue

            kept, total, new_regions = 0, 0, []
            for r in rect_regions(src):
                total += 1
                wh = warp_fn(r['shape_attributes'])
                if wh is None:
                    continue
                nr = copy.deepcopy(r)
                nr['shape_attributes'] = {'name': 'rect', 'x': wh[0], 'y': wh[1],
                                          'width': wh[2], 'height': wh[3]}
                new_regions.append(nr)
                kept += 1
            tgt_meta = via['_via_img_metadata'][t['key']]
            tgt_meta['regions'] = new_regions
            items[ti]['regions'] = tgt_meta['regions']
            rows.append({'target': t['fn'], 'source': src['fn'], 'dist': dist,
                         **stats, 'boxes_in': total, 'boxes_kept': kept})
            if chain:
                manual.add(ti)
        else:
            allowed = None if chain else manual
            si = dist = None
            n = len(items)
            for dd in range(1, max_dist + 1):
                for cand in (ti - dd, ti + dd):
                    if 0 <= cand < n and (allowed is None or cand in allowed) \
                            and len(rect_regions(items[cand])) >= min_source_regions:
                        si, dist = cand, dd
                        break
                if si is not None:
                    break
            if si is None:
                rows.append({'target': t['fn'], 'status': 'no_source',
                             'dist': '', 'source': '', 'boxes_in': 0, 'boxes_kept': 0})
                continue
            src = items[si]
            H, stats = estimate_H_cached(src, t, min_inliers=min_inliers,
                                         min_inlier_ratio=min_inlier_ratio,
                                         allow_affine=allow_affine)
            if H is None:
                rows.append({'target': t['fn'], 'source': src['fn'], 'dist': dist,
                             **stats, 'boxes_in': 0, 'boxes_kept': 0})
                continue

            kept, total, new_regions = 0, 0, []
            for r in rect_regions(src):
                total += 1
                wh = warp_rect(r['shape_attributes'], H, stats['img_w'], stats['img_h'])
                if wh is None:
                    continue
                nr = copy.deepcopy(r)
                nr['shape_attributes'] = {'name': 'rect', 'x': wh[0], 'y': wh[1],
                                          'width': wh[2], 'height': wh[3]}
                new_regions.append(nr)
                kept += 1
            tgt_meta = via['_via_img_metadata'][t['key']]
            tgt_meta['regions'] = new_regions
            items[ti]['regions'] = tgt_meta['regions']
            rows.append({'target': t['fn'], 'source': src['fn'], 'dist': dist,
                         **stats, 'boxes_in': total, 'boxes_kept': kept})
            if chain:
                manual.add(ti)
    return via, rows


def _merge_donors_row_lens(via, items, ti, lo, hi, allowed, min_source_regions,
                           dup_overlap, donor_idx, pair_models, pair_stats):
    """Сумма боксов со всех доноров кластера по предрасчитанным парам с учётом
    дисторсии (_lens_prepass). Логика отчёта и дедупликации — как в
    _merge_donors_row."""
    t = items[ti]
    cands = []
    for si in donor_pool(lo, hi, donor_idx):
        if si == ti:
            continue
        if allowed is not None and si not in allowed:
            continue
        if len(rect_regions(items[si])) < min_source_regions:
            continue
        st = pair_stats.get((si, ti))
        if st is None:
            continue
        gm = st.get('matches', 0)
        if gm > 0:
            cands.append((gm, si))
    cands.sort(reverse=True)
    if not cands:
        return {'target': t['fn'], 'status': 'no_source_in_cluster',
                'dist': '', 'source': '', 'boxes_in': 0, 'boxes_kept': 0}

    collected, donor_stats = [], []
    for rank, (gm, si) in enumerate(cands):
        src = items[si]
        stats = dict(pair_stats[(si, ti)])
        stats['donor'] = src['fn']
        donor_stats.append(stats)
        model = pair_models.get((si, ti))
        if model is None:
            continue
        for r in rect_regions(src):
            wh = warp_box_lens(r['shape_attributes'], model)
            if wh is None:
                continue
            nr = copy.deepcopy(r)
            nr['shape_attributes'] = {'name': 'rect', 'x': wh[0], 'y': wh[1],
                                      'width': wh[2], 'height': wh[3]}
            collected.append({'x': wh[0], 'y': wh[1], 'w': wh[2], 'h': wh[3],
                              'donor': si, 'region': nr})

    n_ok = sum(1 for d in donor_stats if d['status'] == 'ok')
    failed = ';'.join(f"{d['donor']}:{d['status']}"
                      for d in donor_stats if d['status'] != 'ok')
    used = []
    for b in collected:
        fn = items[b['donor']]['fn']
        if fn not in used:
            used.append(fn)

    if not collected:
        st = donor_stats[0]['status'] if donor_stats else 'no_source_in_cluster'
        return {'target': t['fn'], 'status': st, 'dist': '',
                'source': ';'.join(d['donor'] for d in donor_stats),
                'boxes_in': 0, 'boxes_kept': 0, 'dedup_dropped': 0,
                'failed_donors': failed}

    kept_boxes, dropped_boxes = dedup_boxes(collected, dup_overlap)
    tgt_meta = via['_via_img_metadata'][t['key']]
    tgt_meta['regions'] = [b['region'] for b in kept_boxes]
    items[ti]['regions'] = tgt_meta['regions']

    ok_stats = [d for d in donor_stats if d['status'] == 'ok']
    ks = sorted({str(d.get('k')) for d in ok_stats if d.get('k') is not None})
    row = {'target': t['fn'], 'source': ';'.join(used), 'dist': '',
           'status': 'ok' if n_ok == len(cands) else ('ok_partial' if n_ok else 'all_failed'),
           'method': f'multi({n_ok}/{len(cands)})',
           'k': ';'.join(ks),
           'donor_matches': cands[0][0],
           'boxes_in': len(collected), 'boxes_kept': len(kept_boxes),
           'dedup_dropped': len(dropped_boxes),
           'failed_donors': failed}
    if ok_stats:
        row['inliers'] = min(d.get('inliers', 0) for d in ok_stats)
        row['inlier_ratio'] = min(d.get('inlier_ratio', 0) for d in ok_stats)
    return row


def _merge_donors_row(via, items, ti, lo, hi, allowed, min_source_regions,
                      min_inliers, min_inlier_ratio, allow_affine, dup_overlap,
                      donor_idx=None):
    """Сумма боксов со ВСЕХ размеченных доноров кластера + дедупликация.

    Доноры сортируются по числу совпадений дескрипторов с целью (убывание) —
    в этом порядке боксы идут в dedup_boxes, поэтому при дубликате выживает
    бокс с более надёжно сматчившегося донора. max_dist намеренно не режется:
    доноры заданы явно, качество каждой пары гейтится inliers-порогами.
    Несошедшиеся доноры попадают в поле отчёта failed_donors (файл:статус)."""
    t = items[ti]
    cands = []
    for si in donor_pool(lo, hi, donor_idx):
        if si == ti:
            continue
        if allowed is not None and si not in allowed:
            continue
        if len(rect_regions(items[si])) < min_source_regions:
            continue
        gm = len(good_matches(items[ti].get('des'), items[si].get('des')))
        if gm > 0:
            cands.append((gm, si))
    cands.sort(reverse=True)
    if not cands:
        return {'target': t['fn'], 'status': 'no_source_in_cluster',
                'dist': '', 'source': '', 'boxes_in': 0, 'boxes_kept': 0}

    collected, donor_stats = [], []
    for rank, (gm, si) in enumerate(cands):
        src = items[si]
        H, stats = estimate_H_cached(src, t, min_inliers=min_inliers,
                                     min_inlier_ratio=min_inlier_ratio,
                                     allow_affine=allow_affine)
        stats['donor'] = src['fn']
        donor_stats.append(stats)
        if H is None:
            continue
        for r in rect_regions(src):
            wh = warp_rect(r['shape_attributes'], H, stats['img_w'], stats['img_h'])
            if wh is None:
                continue
            nr = copy.deepcopy(r)
            nr['shape_attributes'] = {'name': 'rect', 'x': wh[0], 'y': wh[1],
                                      'width': wh[2], 'height': wh[3]}
            collected.append({'x': wh[0], 'y': wh[1], 'w': wh[2], 'h': wh[3],
                              'donor': si, 'region': nr})

    n_ok = sum(1 for d in donor_stats if d['status'] == 'ok')
    failed = ';'.join(f"{d['donor']}:{d['status']}"
                      for d in donor_stats if d['status'] != 'ok')
    used = []
    for b in collected:
        fn = items[b['donor']]['fn']
        if fn not in used:
            used.append(fn)

    if not collected:
        st = donor_stats[0]['status'] if donor_stats else 'no_source_in_cluster'
        return {'target': t['fn'], 'status': st, 'dist': '',
                'source': ';'.join(d['donor'] for d in donor_stats),
                'boxes_in': 0, 'boxes_kept': 0, 'dedup_dropped': 0,
                'failed_donors': failed}

    kept_boxes, dropped_boxes = dedup_boxes(collected, dup_overlap)
    tgt_meta = via['_via_img_metadata'][t['key']]
    tgt_meta['regions'] = [b['region'] for b in kept_boxes]
    items[ti]['regions'] = tgt_meta['regions']

    ok_stats = [d for d in donor_stats if d['status'] == 'ok']
    row = {'target': t['fn'], 'source': ';'.join(used), 'dist': '',
           'status': 'ok' if n_ok == len(cands) else ('ok_partial' if n_ok else 'all_failed'),
           'method': f'multi({n_ok}/{len(cands)})',
           'donor_matches': cands[0][0],
           'boxes_in': len(collected), 'boxes_kept': len(kept_boxes),
           'dedup_dropped': len(dropped_boxes),
           'failed_donors': failed}
    if ok_stats:
        row['inliers'] = min(d.get('inliers', 0) for d in ok_stats)
        row['inlier_ratio'] = min(d.get('inlier_ratio', 0) for d in ok_stats)
    return row


def draw_previews(via, root, out_dir, targets, max_w=1280):
    """Превью с отрисованными боксами для быстрой визуальной проверки."""
    try:
        import cv2
    except ImportError:
        sys.exit('Нужен OpenCV:  pip install opencv-python-headless')
    os.makedirs(out_dir, exist_ok=True)
    items = build_items(via)
    for ti in targets:
        t = items[ti]
        img = cv2.imread(os.path.join(root, t['fn']))
        if img is None:
            continue
        sc = min(1.0, max_w / img.shape[1])
        if sc < 1.0:
            img = cv2.resize(img, None, fx=sc, fy=sc)
        for r in rect_regions(items[ti]):
            s = r['shape_attributes']
            p0 = (int(s['x'] * sc), int(s['y'] * sc))
            p1 = (int((s['x'] + s['width']) * sc), int((s['y'] + s['height']) * sc))
            cv2.rectangle(img, p0, p1, (0, 255, 0), 2)
            lab = r.get('region_attributes', {}).get('garbage', '')
            if isinstance(lab, dict):
                lab = '+'.join(k for k, v in lab.items() if v)
            cv2.putText(img, str(lab), (p0[0], max(12, p0[1] - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)
        cv2.imwrite(os.path.join(out_dir, t['fn']), img,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 80])


# ----------------------------- план (файл состояния) -----------------------------

def save_plan(path, pack_name, clusters, items, donors_by_cluster, params):
    plan = {'pack': pack_name, 'version': 4, 'params': params,
            'clusters': [{'lo': items[lo]['num'], 'hi': items[hi]['num'],
                          'donors': [items[d]['fn'] for d in donors]}
                         for (lo, hi), donors in zip(clusters, donors_by_cluster)]}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    return plan


def load_plan(path, items):
    with open(path, encoding='utf-8') as f:
        plan = json.load(f)
    spec = ','.join(f"{c['lo']}-{c['hi']}" for c in plan['clusters'])
    clusters = parse_ranges(spec, items)
    return plan, clusters


def ask_yes(prompt, default=True):
    try:
        ans = input(prompt).strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans[0] in ('y', 'д', '1')


def print_clusters(clusters, items, targets=None):
    tset = set(targets or [])
    print(f'Кластеров: {len(clusters)}')
    for ci, (lo, hi) in enumerate(clusters):
        n_tgt = len(tset & set(range(lo, hi + 1)))
        extra = f', целей: {n_tgt}' if targets else ''
        print(f'  кластер {ci + 1}: кадры {items[lo]["num"]}–{items[hi]["num"]} '
              f'({hi - lo + 1} шт.{extra})')


def choose_targets(args, items, clusters, donor_fns):
    """Цели переноса. Приоритет: --targets > --targets-mode > auto
    (nondonors, если есть план/--donors, иначе empty)."""
    if args.targets:
        wanted = set(x.strip() for x in args.targets.split(','))
        tg = [i for i, d in enumerate(items) if d['fn'] in wanted]
        return tg, 'явный список --targets'
    mode = args.targets_mode
    if mode == 'auto':
        mode = 'nondonors' if donor_fns else 'empty'
    if mode == 'nondonors':
        in_cl = set()
        for lo, hi in clusters:
            in_cl.update(range(lo, hi + 1))
        tg = sorted(i for i in in_cl if items[i]['fn'] not in donor_fns)
        n_old = sum(1 for i in tg if len(rect_regions(items[i])) > 0)
        msg = (f'все кадры кластеров, кроме {len(donor_fns)} доноров. '
               f'У {n_old} целей уже есть разметка — она будет ЗАМЕНЕНА перенесённой')
        return tg, msg
    tg = [i for i, d in enumerate(items) if len(rect_regions(d)) == 0]
    return tg, 'только кадры без единого бокса'


# ----------------------------- сценарии -----------------------------

def cmd_plan(args, via, items, root):
    print(f'Проход 1 (O(n)): словарь признаков для {len(items)} кадров...')
    build_dictionary(items, root, max_dim=args.max_dim, nfeatures=args.sig_feats)
    edges, clusters, T = cut_clusters(items, min_edge_matches=args.min_edge_matches,
                                      cut_on_sig=True, cut_k=args.cut_k,
                                      cut_abs=args.cut_abs)
    print(f'Авто-разбиение (порог сигнатур T={T:.3f}):' if T else 'Авто-разбиение:')
    print_clusters(clusters, items)
    if args.edges_csv:
        with open(args.edges_csv, 'w', newline='', encoding='utf-8') as f:
            wr = csv.DictWriter(f, fieldnames=['a', 'b', 'gap', 'good_matches',
                                               'd_thumb', 'd_hist', 'd_sig', 'cut'])
            wr.writeheader()
            for e in edges:
                wr.writerow({**e, 'a': items[e['a']]['fn'], 'b': items[e['b']]['fn'],
                             'd_thumb': _f3(e['d_thumb']), 'd_hist': _f3(e['d_hist']),
                             'd_sig': _f3(e['d_sig'])})
        print('Рёбра: ' + args.edges_csv)

    if not ask_yes('Разделение на кластеры норм? [Y/n] '):
        while True:
            spec = input('Введи границы вручную (например 2685-2730,2731-2912): ')
            try:
                clusters = parse_ranges(spec, items)
                break
            except ValueError as e:
                print('Ошибка: ' + str(e) + '. Ещё раз.')
        print('Принято ручное разбиение:')
        print_clusters(clusters, items)

    print('Подбор кадров под ручную разметку (по сигнатурам)...')
    donors_by_cluster = []
    for ci, (lo, hi) in enumerate(clusters):
        n = hi - lo + 1
        k = args.donors_per_cluster or min(5, max(2, n // 12))
        donors = suggest_donors(items, lo, hi, k)
        poor = coverage_check(items, lo, hi, donors, args.min_edge_matches)
        if poor:
            add = [p for p in poor if p not in donors][:2]
            donors = sorted(donors + add)
            print(f'  кластер {ci + 1}: кадры {[items[p]["fn"] for p in poor]} покрыты слабо, '
                  f'добавил в доноры: {[items[d]["fn"] for d in add]}')
        donors_by_cluster.append(donors)
        fns = [items[d]['fn'] for d in donors]
        print(f'  кластер {ci + 1} ({items[lo]["num"]}–{items[hi]["num"]}, {n} шт.): '
              f'разметить {len(donors)}: ' + ', '.join(fns))

    plan = save_plan(args.plan, os.path.basename(args.json), clusters, items,
                     donors_by_cluster, vars(args))
    donors_txt = os.path.splitext(args.plan)[0] + '_donors.txt'
    with open(donors_txt, 'w', encoding='utf-8') as f:
        for c in plan['clusters']:
            f.write(f"# {c['lo']}-{c['hi']}\n" + '\n'.join(c['donors']) + '\n')
    print(f'\nПлан сохранён: {args.plan}; список кадров: {donors_txt}')
    print('Дальше: разметь эти кадры в VIA, сохрани проект (Project -> Save), затем:')
    print(f'  python via_propagate.py <сохранённый.json> --use-plan {args.plan} '
          f'--root {root} --out filled.json --draw previews')


def cmd_apply(args, via, items, root, clusters, donor_fns=None):
    if donor_fns is None and args.donors:
        donor_fns = set(x.strip() for x in args.donors.split(','))
    donor_idx = None
    if donor_fns:
        known = {d['fn'] for d in items}
        unknown = donor_fns - known
        if unknown:
            print(f'ВНИМАНИЕ: доноры не найдены в проекте: {sorted(unknown)}')
        fn2idx = {d['fn']: i for i, d in enumerate(items)}
        donor_idx = {fn2idx[fn] for fn in donor_fns if fn in fn2idx}
        out_cl = sorted(fn for fn in donor_fns
                        if fn in fn2idx and cluster_of(clusters, fn2idx[fn]) is None)
        if out_cl:
            print('Доноры вне интервалов кластеров: ' + ', '.join(out_cl) +
                  ' — используются как источники для всех целей')
    lens = getattr(args, 'lens_k', 'off')
    if lens not in ('off', 'auto'):
        try:
            lens = float(lens)
        except (TypeError, ValueError):
            sys.exit('--lens-k: ожидается off | auto | число (например -0.08)')
    if lens != 'off':
        print('Учёт дисторсии: ' + ('оценка k по каждому кластеру (auto)'
                                    if lens == 'auto' else f'фиксированный k = {lens:+.4f}'))
    print(f'Словарь признаков для {len(items)} кадров...')
    build_dictionary(items, root, max_dim=args.max_dim, nfeatures=args.sig_feats)
    targets, how = choose_targets(args, items, clusters, donor_fns)
    print(f'Целей: {len(targets)} ({how})')
    print_clusters(clusters, items, targets)
    if args.merge_donors:
        print(f'Режим суммы доноров: на цель переносится объединение боксов всех '
              f'доноров кластера; дубликат = перекрытие >= {args.dup_overlap:.0%} '
              f'площади боксом с другого донора')
    min_src = 1 if donor_fns else args.min_source_regions
    via, rows = propagate(via, root, targets, max_dist=args.max_dist,
                          min_source_regions=min_src,
                          min_inliers=args.min_inliers,
                          min_inlier_ratio=args.min_inlier_ratio,
                          allow_affine=not args.no_affine, chain=args.chain,
                          clusters=clusters, items=items,
                          merge_donors=args.merge_donors,
                          dup_overlap=args.dup_overlap, donor_idx=donor_idx,
                          lens=lens)
    report(args, via, root, targets, rows)


def report(args, via, root, targets, rows):
    cols = ['target', 'source', 'dist', 'status', 'method', 'k', 'donor_matches',
            'matches', 'inliers', 'inlier_ratio', 'boxes_in', 'boxes_kept',
            'dedup_dropped', 'failed_donors']
    n_ok = 0
    for r in rows:
        n_ok += str(r.get('status', '')).startswith('ok')
        suffix = f" (−{r['dedup_dropped']} дублей)" if r.get('dedup_dropped') else ''
        if r.get('failed_donors'):
            suffix += f" (не сошлись: {r['failed_donors']})"
        print('  {target} <- {source} [{status}] {method} dm={donor_matches} '
              'boxes={boxes_kept}/{boxes_in}'.format(**{k: r.get(k, '') for k in cols}) + suffix)
    print(f'Итого: ok {n_ok}/{len(rows)}')
    if args.report:
        with open(args.report, 'w', newline='', encoding='utf-8') as f:
            wr = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
            wr.writeheader()
            wr.writerows(rows)
        print('Отчёт: ' + args.report)
    if args.draw:
        ok = [i for i, r in zip(targets, rows) if str(r.get('status', '')).startswith('ok')]
        draw_previews(via, root, args.draw, ok)
        print('Превью: ' + args.draw)
    if not args.dry:
        out = args.out or re.sub(r'\.json$', '', args.json) + '_filled.json'
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(via, f, ensure_ascii=False, separators=(',', ':'))
        print('Сохранено: ' + out)
    else:
        print('Режим --dry: JSON не сохранялся.')


def _f3(x):
    return round(x, 4) if isinstance(x, float) else x


def main():
    ap = argparse.ArgumentParser(description='Полуавтоматическая разметка паков VIA 2.x')
    ap.add_argument('json', help='проект VIA (json)')
    ap.add_argument('--root', default=None, help='папка с картинками (по умолчанию — из JSON)')
    ap.add_argument('--out', default=None)
    # сценарии
    ap.add_argument('--plan', default=None, help='режим планирования: кластеры + доноры -> файл плана')
    ap.add_argument('--use-plan', default=None, help='режим применения по файлу плана')
    ap.add_argument('--clusters', default=None,
                    help='резерв без плана: границы "2685-2730,2731-2912"')
    ap.add_argument('--donors', default=None,
                    help='явные доноры через запятую (для резервного режима)')
    ap.add_argument('--targets-mode', choices=['auto', 'empty', 'nondonors'], default='auto',
                    help='кто цели: empty = только пустые; nondonors = все кроме доноров')
    # прочее
    ap.add_argument('--targets', default=None, help='явные цели; переопределяет --targets-mode')
    ap.add_argument('--report', default=None)
    ap.add_argument('--draw', default=None)
    ap.add_argument('--edges-csv', default=None)
    ap.add_argument('--max-dist', type=int, default=8)
    ap.add_argument('--min-source-regions', type=int, default=3)
    ap.add_argument('--min-inliers', type=int, default=12)
    ap.add_argument('--min-inlier-ratio', type=float, default=0.25)
    ap.add_argument('--min-edge-matches', type=int, default=25,
                    help='меньше совпадений между соседями = граница кластера')
    ap.add_argument('--cut-k', type=float, default=4.0, help='множитель MAD для сигнатур')
    ap.add_argument('--cut-abs', type=float, default=0.55, help='абсолютный порог сигнатур')
    ap.add_argument('--donors-per-cluster', type=int, default=0,
                    help='сколько кадров предлагать на кластер (0 = авто 2-5)')
    ap.add_argument('--merge-donors', action='store_true',
                    help='на цель переносится СУММА боксов со всех размеченных доноров '
                         'кластера (а не с одного лучшего), дубликаты режутся по --dup-overlap')
    ap.add_argument('--dup-overlap', type=float, default=0.65,
                    help='доля площади бокса (0..1), накрытая боксом с другого донора, '
                         'при которой бокс считается дубликатом и выбрасывается')
    ap.add_argument('--sig-feats', type=int, default=2500)
    ap.add_argument('--max-dim', type=int, default=1600)
    ap.add_argument('--chain', action='store_true')
    ap.add_argument('--lens-k', default='off', metavar='off|auto|K',
                    help='радиальная дисторсия (division model, 1 параметр): '
                         'off — выкл (как раньше); auto — k оценивается по каждому '
                         'кластеру отдельно (медиана парных оценок, затем рефит H); '
                         'число, например -0.08 — фиксированный k для всех пар')
    ap.add_argument('--no-affine', action='store_true')
    ap.add_argument('--dry', action='store_true')
    args = ap.parse_args()

    via = load_via(args.json)
    root = args.root if args.root is not None else default_root(via)
    if not root:
        sys.exit('Не задана папка с картинками (--root), а в JSON default_filepath пуст')
    items = build_items(via)

    if args.plan:
        cmd_plan(args, via, items, root)
        return

    if args.use_plan or args.clusters:
        donor_fns = None
        if args.use_plan:
            plan, clusters = load_plan(args.use_plan, items)
            donor_fns = {fn for c in plan['clusters'] for fn in c['donors']}
            fn2idx = {d['fn']: i for i, d in enumerate(items)}
            for fn in donor_fns:
                i = fn2idx.get(fn)
                if i is None or len(rect_regions(items[i])) == 0:
                    print(f'ВНИМАНИЕ: донор {fn} из плана сейчас не размечен!')
        else:
            try:
                clusters = parse_ranges(args.clusters, items)
            except ValueError as e:
                sys.exit('Ошибка в --clusters: ' + str(e))
        cmd_apply(args, via, items, root, clusters, donor_fns=donor_fns)
        return

    sys.exit('Укажи сценарий: --plan ФАЙЛ (планирование), --use-plan ФАЙЛ '
             '(применение) или --clusters ".." (резерв). --help для справки.')


if __name__ == '__main__':
    main()
