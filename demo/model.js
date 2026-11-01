// One voltage per physical FSR pad. No interpolation into unmeasured areas.
const FSRPressureMap = (() => {
  const channels = [0, 1, 3, 4];
  const ranges = [0.1, 0.5, 1, 2, 3.3];
  const colors = [[232, 242, 251], [112, 205, 224], [247, 218, 111], [241, 139, 74], [190, 44, 54]];
  function color(voltage, maximum) {
    if (!Number.isFinite(voltage) || voltage < 0) return null;
    const scaled = Math.max(0, Math.min(1, voltage / maximum)) * (colors.length - 1);
    const index = Math.min(colors.length - 2, Math.floor(scaled));
    const fraction = scaled - index;
    const rgb = colors[index].map((value, axis) => Math.round(value + fraction * (colors[index + 1][axis] - value)));
    const linear = rgb.map(value => { const c = value / 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
    const luminance = .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2];
    return { background: `rgb(${rgb.join(', ')})`, foreground: luminance < .3 ? '#ffffff' : '#162c3c' };
  }
  function reading(device, now) {
    let state = 'live';
    if (!device) state = 'service';
    else if (!device.active) state = 'disconnected';
    else if (!device.latest) state = 'waiting';
    else if (!Number.isFinite(device.latest.pc_epoch_ms) || now - device.latest.pc_epoch_ms > 1500 || now - device.latest.pc_epoch_ms < -1000) state = 'stale';
    const values = channels.map(channel => {
      const value = state === 'live' ? device.latest[`F${channel}_V`] : null;
      return Number.isFinite(value) && value >= 0 && value <= 3.3 + 1e-6 ? value : null;
    });
    const valid = values.filter(value => value !== null);
    const peak = valid.length ? Math.max(...valid) : null;
    const peaks = peak === null ? [] : channels.filter((_, index) => values[index] === peak);
    return { state, values, valid: valid.length, peak, peaks, demo: device?.source === 'demo' };
  }
  return { channels, ranges, color, reading };
})();

