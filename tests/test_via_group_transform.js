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
global._via_canvas_regions = REGS.map(() => ({ is_user_selected: false }));
global._via_canvas_scale = 1;
global._via_reg_canvas = {
  style: {}, width: 1000, height: 800,
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 1000, height: 800 }),
  getContext: () => ({ save(){}, restore(){}, strokeRect(){}, setLineDash(){} })
};
global._via_load_canvas_regions = () => {};
global._via_redraw_reg_canvas = () => {};

let fails = 0, prevented = false;
const ok = (c, n) => { if (!c) { fails++; console.log('FAIL:', n); } else console.log('ok:', n); };
const ev = o => Object.assign({ target: {tagName:'CANVAS'},
  preventDefault(){ prevented = true; }, stopPropagation(){}, button:0 }, o);

async function main() {
  const GT = require('../via_group_transform.js');

  // --- чистая геометрия ---
  ok(GT.boxCaptured(100,100,50,40, 0,0,200,200), 'рамка: бокс внутри области — захвачен');
  ok(!GT.boxCaptured(300,300,50,40, 0,0,200,200), 'рамка: бокс снаружи — не захвачен');
  ok(GT.boxCaptured(180,100,50,40, 0,0,200,200), 'рамка: бокс пересекает границу — захвачен');
  ok(!GT.boxCaptured(100,100,500,500, 150,150,160,160), 'рамка: крошечная область внутри огромного бокса — НЕ захвачен');
  let bs = [{cx:0,cy:0,w:100,h:50},{cx:200,cy:0,w:60,h:30}];
  let c2 = JSON.parse(JSON.stringify(bs));
  c2.forEach(x => GT.xfScale(x, {x:100,y:0}, 2, null, true));
  ok(Math.abs(c2[0].cx) < 1e-9 && c2[0].w === 200, 'xfScale индивид.: центры на месте, размеры x2');

  // --- интеграция (заглушка VIA) ---
  await new Promise(r => setTimeout(r, 700));
  const kd = handlers['keydown'][0], mm = handlers['mousemove'][0],
        md = handlers['mousedown'][0], mu = handlers['mouseup'][0];

  let n0 = messages.length;
  prevented = false; kd(ev({ key: 'z', code: 'KeyZ' }));
  ok(!prevented && messages.length === n0, 'z не перехвачена (уйдёт в VIA как отмена)');
  prevented = false; kd(ev({ key: 'x', code: 'KeyX' }));
  ok(!prevented && messages.length === n0, 'x не перехвачена');

  kd(ev({ key: 'g', code: 'KeyG' }));
  mm(ev({ clientX: 100, clientY: 100 })); mm(ev({ clientX: 300, clientY: 100 }));
  ok(REGS[0].shape_attributes.x === 100 && REGS[1].shape_attributes.x === 500,
     'без выделения боксы не двигаются');
  ok(messages.some(m => m.includes('Ничего не выделено')), 'есть подсказка про выделение');

  kd(ev({ key: 'b', code: 'KeyB' }));
  md(ev({ clientX: 50, clientY: 50 }));
  mm(ev({ clientX: 400, clientY: 300 }));
  mu(ev({ clientX: 400, clientY: 300 }));
  ok(messages.some(m => m.includes('Выделено боксов: 1')), 'рамка: выделен 1 бокс');

  global._via_canvas_regions.forEach(r => r.is_user_selected = false);  // "ломаем" флаги VIA
  kd(ev({ key: 'п', code: 'KeyG' }));   // G в русской раскладке
  mm(ev({ clientX: 500, clientY: 400 }));
  mm(ev({ clientX: 700, clientY: 400 }));
  md(ev({ clientX: 700, clientY: 400, button: 0 }));
  ok(REGS[0].shape_attributes.x === 300, 'G двигает выделенный рамкой бокс (RU-раскладка, GT_SEL)');
  ok(REGS[1].shape_attributes.x === 500, 'невыделенный бокс не тронут');

  kd(ev({ key: 'u', code: 'KeyU' }));
  ok(REGS[0].shape_attributes.x === 100, 'U откатил');

  global._via_canvas_regions.forEach(r => r.is_user_selected = false);
  global._via_canvas_regions[1].is_user_selected = true;
  kd(ev({ key: 'g', code: 'KeyG' }));
  mm(ev({ clientX: 500, clientY: 400 }));
  mm(ev({ clientX: 550, clientY: 400 }));
  md(ev({ clientX: 550, clientY: 400, button: 0 }));
  ok(REGS[1].shape_attributes.x === 550 && REGS[0].shape_attributes.x === 100,
     'штатное выделение VIA приоритетнее GT_SEL');

  global._via_canvas_regions.forEach(r => r.is_user_selected = false);
  kd(ev({ key: 'b', code: 'KeyB' }));
  md(ev({ clientX: 50, clientY: 50 })); mm(ev({ clientX: 400, clientY: 300 })); mu(ev({ clientX: 400, clientY: 300 }));
  global._via_canvas_regions.forEach(r => r.is_user_selected = false);
  kd(ev({ key: 'Escape', code: 'Escape' }));
  ok(messages.some(m => m.includes('снято')), 'Esc снял GT-выделение');

  kd(ev({ key: '`', code: 'Backquote' }));
  ok(messages[messages.length-1].includes('gt_sel=0'), 'диагностика по `');

  const before = messages.length;
  kd(ev({ key: 'g', code: 'KeyG', ctrlKey: true }));
  ok(messages.length === before, 'Ctrl+G не перехватывается');

  console.log(fails ? `\n${fails} FAIL` : '\nВсе тесты via_group_transform пройдены');
  process.exit(fails ? 1 : 0);
}
main().catch(e => { console.log('CRASH:', e); process.exit(1); });
