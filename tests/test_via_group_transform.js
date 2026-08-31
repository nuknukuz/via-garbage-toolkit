// Тесты via_group_transform.js на заглушке VIA. Запуск из корня репозитория:
//   node tests/test_via_group_transform.js
'use strict';

const handlers = {};
const messages = [];
global.show_message = t => messages.push(t);
global.document = {
  readyState: 'complete',
  addEventListener: (type, fn) => { (handlers[type] = handlers[type] || []).push(fn); }
};
global.window = { addEventListener: (type, fn) => { (handlers[type] = handlers[type] || []).push(fn); } };

const REGS = [
  { shape_attributes: {name:'rect', x:100, y:100, width:50, height:40}, region_attributes:{} },
  { shape_attributes: {name:'rect', x:500, y:400, width:80, height:60}, region_attributes:{} },
];
global._via_image_id = 'img1.jpg100';
global._via_img_metadata = { 'img1.jpg100': { filename:'img1.jpg', regions: REGS } };

// Заглушка canvas-регионов: VIA хранит копию координат в canvas-пространстве
// (canvas = image * CANVAS_K) и флаги выделения.
let CANVAS_K = 1;
function syncCanvasRegions() {
  global._via_canvas_regions = REGS.map(r => ({
    is_user_selected: false,
    shape_attributes: { name:'rect',
      x: r.shape_attributes.x * CANVAS_K, y: r.shape_attributes.y * CANVAS_K,
      width: r.shape_attributes.width * CANVAS_K, height: r.shape_attributes.height * CANVAS_K }
  }));
}
syncCanvasRegions();
global._via_load_canvas_regions = () => syncCanvasRegions();
global._via_canvas_scale = 1;   // v2.3 не должна на него опираться
global._via_reg_canvas = {
  style: {}, width: 1000, height: 800,
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 1000, height: 800 }),
  getContext: () => ({ save(){}, restore(){}, strokeRect(){}, setLineDash(){} })
};
global._via_redraw_reg_canvas = () => {};

let fails = 0, prevented = false;
const ok = (c, n) => { if (!c) { fails++; console.log('FAIL:', n); } else console.log('ok:', n); };
const ev = o => Object.assign({ target: {tagName:'CANVAS'},
  preventDefault(){ prevented = true; }, stopPropagation(){}, button:0 }, o);

const clearViaSel = () => global._via_canvas_regions.forEach(r => r.is_user_selected = false);
const lastMsg = () => messages[messages.length - 1];

function dragSelect(x0, y0, x1, y1, shift) {
  const kd = handlers['keydown'][0], mm = handlers['mousemove'][0],
        md = handlers['mousedown'][0], mu = handlers['mouseup'][0];
  kd(ev({ key: 'b', code: 'KeyB', shiftKey: !!shift }));
  md(ev({ clientX: x0, clientY: y0 }));
  mm(ev({ clientX: x1, clientY: y1 }));
  mu(ev({ clientX: x1, clientY: y1 }));
}

async function main() {
  const GT = require('../via_group_transform.js');

  // --- чистая геометрия ---
  ok(GT.boxCaptured(100,100,50,40, 0,0,200,200), 'рамка: бокс внутри области — захвачен');
  ok(!GT.boxCaptured(300,300,50,40, 0,0,200,200), 'рамка: бокс снаружи — не захвачен');
  ok(GT.boxCaptured(180,100,50,40, 0,0,200,200), 'рамка: бокс пересекает границу — захвачен');
  ok(!GT.boxCaptured(100,100,500,500, 150,150,160,160), 'рамка: крошечная область внутри огромного бокса — НЕ захвачен');
  let c2 = [{cx:0,cy:0,w:100,h:50},{cx:200,cy:0,w:60,h:30}];
  c2.forEach(x => GT.xfScale(x, {x:100,y:0}, 2, null, true));
  ok(Math.abs(c2[0].cx) < 1e-9 && c2[0].w === 200, 'xfScale индивид.: центры на месте, размеры x2');

  // --- интеграция (заглушка VIA) ---
  await new Promise(r => setTimeout(r, 700));
  const kd = handlers['keydown'][0], mm = handlers['mousemove'][0],
        md = handlers['mousedown'][0], mu = handlers['mouseup'][0];

  // клавиши вне режима: y свободна, x заглушен, z — откат
  let n0 = messages.length;
  prevented = false; kd(ev({ key: 'y', code: 'KeyY' }));
  ok(!prevented && messages.length === n0, 'y вне режима не перехватывается');
  prevented = false; kd(ev({ key: 'x', code: 'KeyX' }));
  ok(prevented && lastMsg().includes('X вне режима'), 'x вне режима заглушен, с подсказкой');
  prevented = false; kd(ev({ key: 'z', code: 'KeyZ' }));
  ok(prevented && lastMsg().includes('Откатывать нечего'), 'z вне режима — откат (история пуста)');

  // без выделения боксы не двигаются
  kd(ev({ key: 'g', code: 'KeyG' }));
  mm(ev({ clientX: 100, clientY: 100 })); mm(ev({ clientX: 300, clientY: 100 }));
  ok(REGS[0].shape_attributes.x === 100 && REGS[1].shape_attributes.x === 500,
     'без выделения боксы не двигаются');
  ok(messages.some(m => m.includes('Ничего не выделено')), 'есть подсказка про выделение');

  // рамка B, масштаб 1:1
  dragSelect(50, 50, 400, 300);
  ok(lastMsg().includes('Выделено боксов: 1'), 'рамка: выделен 1 бокс');

  // G двигает выделенный рамкой бокс (RU-раскладка, GT_SEL)
  clearViaSel();  // "ломаем" флаги VIA
  kd(ev({ key: 'п', code: 'KeyG' }));
  mm(ev({ clientX: 500, clientY: 400 }));
  mm(ev({ clientX: 700, clientY: 400 }));
  md(ev({ clientX: 700, clientY: 400, button: 0 }));
  ok(REGS[0].shape_attributes.x === 300, 'G двигает выделенный рамкой бокс (RU-раскладка, GT_SEL)');
  ok(REGS[1].shape_attributes.x === 500, 'невыделенный бокс не тронут');

  kd(ev({ key: 'z', code: 'KeyZ' }));
  ok(REGS[0].shape_attributes.x === 100 && lastMsg().includes('Откат выполнен'), 'Z откатил перенос');

  // штатное выделение VIA приоритетнее GT_SEL
  clearViaSel();
  global._via_canvas_regions[1].is_user_selected = true;
  kd(ev({ key: 'g', code: 'KeyG' }));
  mm(ev({ clientX: 500, clientY: 400 }));
  mm(ev({ clientX: 550, clientY: 400 }));
  md(ev({ clientX: 550, clientY: 400, button: 0 }));
  ok(REGS[1].shape_attributes.x === 550 && REGS[0].shape_attributes.x === 100,
     'штатное выделение VIA приоритетнее GT_SEL');
  kd(ev({ key: 'u', code: 'KeyU' }));  // вернуть 500, U — алиас отката
  ok(REGS[1].shape_attributes.x === 500, 'U работает как алиас отката');

  // Esc снимает GT-выделение, диагностика
  clearViaSel();
  dragSelect(50, 50, 400, 300);
  clearViaSel();
  kd(ev({ key: 'Escape', code: 'Escape' }));
  ok(messages.some(m => m.includes('снято')), 'Esc снял GT-выделение');
  kd(ev({ key: '`', code: 'Backquote' }));
  ok(lastMsg().includes('gt_sel=0') && lastMsg().includes('R='), 'диагностика по ` (с R и списком via_sel)');

  const before = messages.length;
  kd(ev({ key: 'g', code: 'KeyG', ctrlKey: true }));
  ok(messages.length === before, 'Ctrl+G не перехватывается');

  // --- зум: canvas = image x2, backing store 2000x1600 при CSS 1000x800,
  //     а _via_canvas_scale намеренно мусорный — v2.3 обязана его игнорировать ---
  CANVAS_K = 2; syncCanvasRegions();
  global._via_reg_canvas.width = 2000; global._via_reg_canvas.height = 1600;
  global._via_canvas_scale = 999;
  ok(Math.abs(GT.canvasToImageScale() - 0.5) < 1e-9, 'эмпирический R = 0.5 при canvas = image x2');

  clearViaSel();
  kd(ev({ key: 'Escape', code: 'Escape' }));  // на всякий случай чистим GT_SEL
  dragSelect(25, 25, 200, 150);               // client -> canvas (50,50)-(400,300)
  ok(lastMsg().includes('Выделено боксов: 1'), 'рамка B при зуме выделяет ровно 1 бокс');

  clearViaSel();
  kd(ev({ key: 'g', code: 'KeyG' }));
  mm(ev({ clientX: 250, clientY: 200 }));     // canvas (500,400) -> img (250,200): старт
  mm(ev({ clientX: 350, clientY: 200 }));     // img (350,200): дельта (100,0)
  md(ev({ clientX: 350, clientY: 200, button: 0 }));
  ok(REGS[0].shape_attributes.x === 200, 'G при зуме: сдвиг мыши пересчитан в image-координаты');
  kd(ev({ key: 'z', code: 'KeyZ' }));
  ok(REGS[0].shape_attributes.x === 100, 'Z откатил перенос при зуме');

  // возврат к 1:1
  CANVAS_K = 1; syncCanvasRegions();
  global._via_reg_canvas.width = 1000; global._via_reg_canvas.height = 800;
  global._via_canvas_scale = 1;

  // Shift+B дособирает к GT_SEL, даже если флаги VIA потеряны
  clearViaSel();
  dragSelect(50, 50, 400, 300, false);        // выделен бокс 0
  clearViaSel();                              // флаги VIA потеряны, GT_SEL живёт
  dragSelect(450, 350, 600, 500, true);       // Shift+B по боксу 1
  ok(lastMsg().includes('Выделено боксов: 2'), 'Shift+B дособирает к GT_SEL при потерянных флагах VIA');

  // сообщение режима показывает число боксов и предупреждает про "все"
  kd(ev({ key: 'g', code: 'KeyG' }));
  ok(lastMsg().includes('2 бокс') && lastMsg().includes('ВСЕ'),
     'режим сообщает число боксов и предупреждает, если выделены все');
  kd(ev({ key: 'z', code: 'KeyZ' }));         // Z в режиме — отмена режима
  ok(lastMsg().includes('Отменено') && REGS[0].shape_attributes.x === 100,
     'Z в режиме отменяет режим (как Esc)');

  console.log(fails ? `\n${fails} FAIL` : '\nВсе тесты via_group_transform пройдены');
  process.exit(fails ? 1 : 0);
}
main().catch(e => { console.log('CRASH:', e); process.exit(1); });
