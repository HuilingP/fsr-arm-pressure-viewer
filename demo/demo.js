(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const channels = FSRPressureMap.channels;
  let scene = 'balanced', paused = false, maximum = 3.3, elapsed = 0;
  let values = Array(8).fill(0);
  let previous = performance.now();
  const buttons = [...document.querySelectorAll('[data-scene]')];
  function simulated(t) {
    if (scene === 'manual') return values;
    if (scene === 'scan') {
      const index = Math.floor(t / 1.25) % 8;
      return Array.from({ length: 8 }, (_, i) => i === index ? 2.55 + .35 * Math.sin(t * 3) : .025);
    }
    const base = scene === 'left' ? [2.5, 2.9, 1.3, .5, .09, .12, .08, .05]
      : scene === 'right' ? [.05, .08, .12, .09, .5, 1.3, 2.9, 2.5]
      : [.8, 1.5, 1.7, 1.1, 1.1, 1.7, 1.5, .8];
    return base.map((v, i) => Math.max(0, Math.min(3.3, v + .09 * Math.sin(t * 1.5 + i * .6))));
  }
  function draw() {
    $('pressureMid').textContent = `${(maximum / 2).toFixed(2)} V`;
    $('pressureMax').textContent = `${maximum.toFixed(2)} V`;
    for (const [sideIndex, slot] of ['A', 'B'].entries()) {
      const slice = values.slice(sideIndex * 4, sideIndex * 4 + 4);
      const peak = Math.max(...slice);
      $('pressureState' + slot).textContent = `模拟数据 · ${scene === 'manual' ? '手动设置' : paused ? '已暂停' : '播放中'}`;
      $('pressureState' + slot).className = 'pressure-connection demo-label';
      channels.forEach((channel, index) => {
        const value = slice[index], paint = FSRPressureMap.color(value, maximum);
        const tile = $(`pressure${slot}${channel}`);
        tile.classList.remove('unavailable');
        tile.classList.toggle('over-range', value > maximum);
        tile.style.backgroundColor = paint.background;
        tile.style.color = paint.foreground;
        $(`voltage${slot}${channel}`).textContent = value.toFixed(3);
        tile.setAttribute('aria-label', `${slot === 'A' ? '左侧' : '右侧'} F${channel}，模拟电压 ${value.toFixed(3)} V`);
        $(`slider${slot}${channel}`).value = value.toFixed(2);
        $(`sliderValue${slot}${channel}`).textContent = `${value.toFixed(2)} V`;
      });
      const peaks = channels.filter((_, index) => slice[index] === peak);
      $('pressurePeak' + slot).textContent = `${peaks.map(c => 'F' + c).join(' / ')} · ${peak.toFixed(3)} V`;
      $('pressureDetail' + slot).textContent = '浏览器生成的模拟电压 · 非真实采样';
    }
    $('pressureNotice').textContent = values.some(value => value > maximum)
      ? '部分模拟电压超过颜色上限，已显示最暖色；数值仍保留原值。可调高标尺。'
      : '切换支撑场景，或用“手动调节”设置各片电压，观察颜色与布局的对应关系。';
  }
  function selectScene(next) {
    scene = next;
    buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.scene === scene)));
    $('manualControls').hidden = scene !== 'manual';
    $('pauseDemo').disabled = scene === 'manual';
    values = simulated(elapsed);
    draw();
  }
  buttons.forEach(button => button.addEventListener('click', () => selectScene(button.dataset.scene)));
  $('pauseDemo').addEventListener('click', () => {
    paused = !paused;
    $('pauseDemo').textContent = paused ? '继续动画' : '暂停动画';
    $('pauseDemo').setAttribute('aria-pressed', String(paused));
    draw();
  });
  $('pressureRange').addEventListener('change', () => {
    const value = Number($('pressureRange').value);
    if (FSRPressureMap.ranges.includes(value)) maximum = value;
    draw();
  });
  ['A', 'B'].forEach((slot, sideIndex) => channels.forEach((channel, index) => {
    $(`slider${slot}${channel}`).addEventListener('input', event => {
      values[sideIndex * 4 + index] = Number(event.target.value);
      draw();
    });
  }));
  selectScene('balanced');
  setInterval(() => {
    const now = performance.now(), dt = Math.min(.2, (now - previous) / 1000);
    previous = now;
    if (!paused && scene !== 'manual' && !document.hidden) {
      elapsed += dt;
      values = simulated(elapsed);
      draw();
    }
  }, 100);
})();
