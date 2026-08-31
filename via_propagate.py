#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
via_propagate.py v4.1 — полуавтоматическая разметка паков кадров VIA 2.x.

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
     - доноры внутри кластера выбираются по максимуму совпадений дескрипторов;
     - гомография — по кэшу словаря, без повторного чтения файлов.

  Резерв (если план потерян/терминал слетел):
       python via_propagate.py marked.json --clusters "2685-2730,2731-2912" --donors "garbage.0002690.jpg,garbage.0002738.jpg" --root . --out filled.json

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


def pick_donor(items, ti, lo, hi, min_regions, max_dist, ratio=0.78, allowed=None):
    """Донор для цели: максимум совпадений дескрипторов, при равенстве — ближайший."""
    best = None
    for si in range(lo, hi + 1):
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


# ----------------------------- перенос -----------------------------

def propagate(via, root, targets, max_dist=8, min_source_regions=3,
              min_inliers=12, min_inlier_ratio=0.25, allow_affine=True,
              chain=False, clusters=None, items=None):
    try:
        import cv2  # noqa: F401
    except ImportError:
        sys.exit('Нужен OpenCV:  pip install opencv-python-headless')

    if items is None:
        items = build_items(via)
    manual = set(range(len(items))) - set(targets)
    rows = []

    for ti in targets:
        t = items[ti]
        if clusters is not None:
            cl = cluster_of(clusters, ti)
            lo, hi = cl if cl else (ti, ti)
            allowed = None if chain else manual
            si, donor_score = pick_donor(items, ti, lo, hi, min_source_regions, max_dist,
                                         allowed=allowed)
            if si is None:
                rows.append({'target': t['fn'], 'status': 'no_source_in_cluster',
                             'dist': '', 'source': '', 'boxes_in': 0, 'boxes_kept': 0})
                continue
            dist = abs(si - ti)
            src = items[si]
            H, stats = estimate_H_cached(src, t, min_inliers=min_inliers,
                                         min_inlier_ratio=min_inlier_ratio,
                                         allow_affine=allow_affine)
            stats['donor_matches'] = donor_score
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
    if donor_fns:
        known = {d['fn'] for d in items}
        unknown = donor_fns - known
        if unknown:
            print(f'ВНИМАНИЕ: доноры не найдены в проекте: {sorted(unknown)}')
    print(f'Словарь признаков для {len(items)} кадров...')
    build_dictionary(items, root, max_dim=args.max_dim, nfeatures=args.sig_feats)
    targets, how = choose_targets(args, items, clusters, donor_fns)
    print(f'Целей: {len(targets)} ({how})')
    print_clusters(clusters, items, targets)
    min_src = 1 if donor_fns else args.min_source_regions
    via, rows = propagate(via, root, targets, max_dist=args.max_dist,
                          min_source_regions=min_src,
                          min_inliers=args.min_inliers,
                          min_inlier_ratio=args.min_inlier_ratio,
                          allow_affine=not args.no_affine, chain=args.chain,
                          clusters=clusters, items=items)
    report(args, via, root, targets, rows)


def report(args, via, root, targets, rows):
    cols = ['target', 'source', 'dist', 'status', 'method', 'donor_matches',
            'matches', 'inliers', 'inlier_ratio', 'boxes_in', 'boxes_kept']
    n_ok = 0
    for r in rows:
        n_ok += r.get('status') == 'ok'
        print('  {target} <- {source} [{status}] {method} dm={donor_matches} '
              'boxes={boxes_kept}/{boxes_in}'.format(**{k: r.get(k, '') for k in cols}))
    print(f'Итого: ok {n_ok}/{len(rows)}')
    if args.report:
        with open(args.report, 'w', newline='', encoding='utf-8') as f:
            wr = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
            wr.writeheader()
            wr.writerows(rows)
        print('Отчёт: ' + args.report)
    if args.draw:
        ok = [i for i, r in zip(targets, rows) if r.get('status') == 'ok']
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
    ap.add_argument('--sig-feats', type=int, default=2500)
    ap.add_argument('--max-dim', type=int, default=1600)
    ap.add_argument('--chain', action='store_true')
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
