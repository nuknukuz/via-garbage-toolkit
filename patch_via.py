#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""patch_via.py — подключает via_group_transform.js к via.html.

Использование: положить рядом с via.html и via_group_transform.js, затем
    python patch_via.py via.html
Создаёт бэкап via.html.bak и правит via.html на месте. Повторный запуск
ничего не меняет. Работает в обоих случаях:
  - нашёлся <script src="...via...js">  -> добавляет ссылку на внешний файл
  - однофайловая сборка                 -> встраивает код перед </body>
"""
import re
import shutil
import sys
from pathlib import Path

TAG = '<script src="via_group_transform.js"></script>'
MARK = 'via_group_transform'


def main():
    html = Path(sys.argv[1] if len(sys.argv) > 1 else 'via.html')
    if not html.exists():
        sys.exit(f'Не найден файл: {html}')
    ext = html.with_name('via_group_transform.js')
    text = html.read_text(encoding='utf-8', errors='replace')

    if MARK in text:
        print('Уже пропатчен — ничего не делаю.')
        return

    shutil.copy2(html, html.with_name(html.name + '.bak'))

    m = re.search(r'<script[^>]*src=["\'][^"\']*via[^"\']*\.js[^"\']*["\'][^>]*>\s*</script>',
                  text, re.I)
    if m:
        text = text[:m.end()] + '\n' + TAG + text[m.end():]
        mode = 'ссылка на внешний файл после тега via.js'
    else:
        if not ext.exists():
            sys.exit(f'Не найден {ext} — положи его рядом с via.html')
        code = ext.read_text(encoding='utf-8')
        block = '<script>\n' + code + '\n</script>\n'
        m2 = re.search(r'</body\s*>', text, re.I)
        if m2:
            text = text[:m2.start()] + block + text[m2.start():]
            mode = 'код встроен перед </body>'
        else:
            text = text.rstrip() + '\n' + block
            mode = 'код дописан в конец файла'

    html.write_text(text, encoding='utf-8')
    print(f'Готово: {html} ({mode}). Бэкап: {html.name}.bak')


if __name__ == '__main__':
    main()
