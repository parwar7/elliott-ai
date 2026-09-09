import fs from "node:fs";

const targets = await fetch("http://127.0.0.1:9222/json/list").then((response) => response.json());
const target = targets.find((item) => item.type === "page" && item.url.includes("tradingview.com/chart/"));
if (!target) throw new Error("No TradingView chart target found.");

const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});

let nextId = 1;
const pending = new Map();
socket.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  const waiter = pending.get(message.id);
  if (!waiter) return;
  pending.delete(message.id);
  if (message.error) waiter.reject(new Error(message.error.message));
  else waiter.resolve(message.result);
});

function send(method, params = {}) {
  const id = nextId++;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

async function evaluate(expression) {
  const result = await send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true,
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
}

await send("Runtime.enable");
await send("Page.enable");

const [command, ...args] = process.argv.slice(2);
if (command === "screenshot") {
  const output = args[0];
  const result = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
  fs.writeFileSync(output, Buffer.from(result.data, "base64"));
  console.log(output);
} else if (command === "download") {
  const outputDir = args[0];
  await send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: outputDir });
  const point = await evaluate(`(() => {
    const nodes = Array.from(document.querySelectorAll('button,[role="button"],a,div'));
    const confirmTargets = nodes.filter((element) => (element.innerText || '').trim() === 'Download');
    const dataTargets = nodes.filter((element) => (element.innerText || '').trim() === 'Download data');
    const targets = confirmTargets.length ? confirmTargets : dataTargets;
    if (!targets.length) return null;
    const target = targets.sort((a, b) => {
      const ar = a.getBoundingClientRect();
      const br = b.getBoundingClientRect();
      return (ar.width * ar.height) - (br.width * br.height);
    })[0];
    const rect = target.getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
  })()`);
  if (point) {
    await send("Input.dispatchMouseEvent", { type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1 });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1 });
  }
  await new Promise((resolve) => setTimeout(resolve, 1000));
  const confirmPoint = await evaluate(`(() => {
    const nodes = Array.from(document.querySelectorAll('button,[role="button"],a,div'));
    const targets = nodes.filter((element) => (element.innerText || '').trim() === 'Download');
    if (!targets.length) return null;
    const target = targets.sort((a, b) => {
      const ar = a.getBoundingClientRect();
      const br = b.getBoundingClientRect();
      return (ar.width * ar.height) - (br.width * br.height);
    })[0];
    const rect = target.getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
  })()`);
  if (confirmPoint) {
    await send("Input.dispatchMouseEvent", { type: "mousePressed", x: confirmPoint.x, y: confirmPoint.y, button: "left", clickCount: 1 });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: confirmPoint.x, y: confirmPoint.y, button: "left", clickCount: 1 });
  }
  await new Promise((resolve) => setTimeout(resolve, 5000));
  console.log(JSON.stringify({ outputDir, point, confirmPoint }));
} else if (command === "dump") {
  const items = await evaluate(`(() => Array.from(document.querySelectorAll('button,[role="button"],[role="menuitem"],[role="tab"]'))
    .filter((element) => { const r = element.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
    .map((element) => ({
      tag: element.tagName,
      text: (element.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 100),
      aria: element.getAttribute('aria-label'),
      title: element.getAttribute('title'),
      role: element.getAttribute('role'),
      rect: (() => { const r = element.getBoundingClientRect(); return { x:r.x, y:r.y, w:r.width, h:r.height }; })(),
    })).slice(0, 500))()`);
  console.log(JSON.stringify(items, null, 2));
} else if (command === "click") {
  const needle = JSON.stringify(args.join(" "));
  const result = await evaluate(`(() => {
    const needle = ${needle}.toLowerCase();
    const nodes = Array.from(document.querySelectorAll('button,[role="button"],[role="menuitem"],[role="tab"]'));
    const matches = nodes.filter((element) => {
      const r = element.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return false;
      const values = [element.getAttribute('aria-label'), element.getAttribute('title'), element.innerText]
        .filter(Boolean).map((value) => value.trim().toLowerCase());
      return values.some((value) => value === needle || value.includes(needle));
    });
    if (matches.length !== 1) return { ok:false, count:matches.length, labels:matches.slice(0,10).map((e) => e.getAttribute('aria-label') || e.getAttribute('title') || e.innerText) };
    matches[0].click();
    return { ok:true, label:matches[0].getAttribute('aria-label') || matches[0].getAttribute('title') || matches[0].innerText };
  })()`);
  console.log(JSON.stringify(result));
} else if (command === "mouse") {
  const x = Number(args[0]);
  const y = Number(args[1]);
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
  console.log(JSON.stringify({ x, y }));
} else if (command === "rightclick") {
  const x = Number(args[0]);
  const y = Number(args[1]);
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "right", clickCount: 1 });
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "right", clickCount: 1 });
  console.log(JSON.stringify({ x, y, button: "right" }));
} else if (command === "drag") {
  const [x1, y1, x2, y2] = args.map(Number);
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x: x1, y: y1, button: "left", clickCount: 1 });
  for (let step = 1; step <= 12; step++) {
    const x = x1 + ((x2 - x1) * step) / 12;
    const y = y1 + ((y2 - y1) * step) / 12;
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "left" });
  }
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: x2, y: y2, button: "left", clickCount: 1 });
  console.log(JSON.stringify({ x1, y1, x2, y2 }));
} else if (command === "wheel") {
  const x = Number(args[0]);
  const y = Number(args[1]);
  const deltaY = Number(args[2]);
  await send("Input.dispatchMouseEvent", { type: "mouseWheel", x, y, deltaX: 0, deltaY });
  console.log(JSON.stringify({ x, y, deltaY }));
} else if (command === "type") {
  await send("Input.insertText", { text: args.join(" ") });
} else if (command === "typefile") {
  const input = fs.readFileSync(args[0], "utf8");
  const focused = await evaluate(`(() => {
    const target = document.querySelector('textarea[aria-label^="Editor content"]');
    if (!target) return false;
    target.focus();
    return document.activeElement === target;
  })()`);
  if (!focused) throw new Error("Pine editor textarea could not be focused.");
  await send("Input.insertText", { text: input });
} else if (command === "pastefile") {
  const input = fs.readFileSync(args[0], "utf8");
  const result = await evaluate(`(() => {
    const target = document.querySelector('textarea[aria-label^="Editor content"]');
    if (!target) return { ok: false, reason: 'Editor textarea not found' };
    target.focus();
    const transfer = new DataTransfer();
    transfer.setData('text/plain', ${JSON.stringify(input)});
    const accepted = target.dispatchEvent(new ClipboardEvent('paste', {
      clipboardData: transfer,
      bubbles: true,
      cancelable: true,
    }));
    return { ok: true, accepted };
  })()`);
  console.log(JSON.stringify(result));
} else if (command === "setinput") {
  const selector = args[0];
  const value = args.slice(1).join(" ");
  const result = await evaluate(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) return { ok: false, selector: ${JSON.stringify(selector)} };
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(element, ${JSON.stringify(value)});
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));
    element.focus();
    return { ok: true, value: element.value };
  })()`);
  console.log(JSON.stringify(result));
} else if (command === "key") {
  const key = args[0];
  await send("Input.dispatchKeyEvent", { type: "keyDown", key });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key });
} else if (command === "hotkey") {
  const key = args[0];
  const modifiers = args.slice(1).reduce((value, item) => value | ({ Alt: 1, Control: 2, Meta: 4, Shift: 8 }[item] || 0), 0);
  await send("Input.dispatchKeyEvent", { type: "keyDown", key, modifiers });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key, modifiers });
} else if (command === "pasteclipboard") {
  const focused = await evaluate(`(() => {
    const target = document.querySelector('textarea[aria-label^="Editor content"]');
    if (!target) return false;
    target.focus();
    return document.activeElement === target;
  })()`);
  if (!focused) throw new Error("Pine editor textarea could not be focused.");
  await send("Input.dispatchKeyEvent", {
    type: "rawKeyDown",
    key: "v",
    code: "KeyV",
    windowsVirtualKeyCode: 86,
    nativeVirtualKeyCode: 86,
    modifiers: 2,
    commands: ["paste"],
  });
  await send("Input.dispatchKeyEvent", {
    type: "keyUp",
    key: "v",
    code: "KeyV",
    windowsVirtualKeyCode: 86,
    nativeVirtualKeyCode: 86,
    modifiers: 2,
  });
} else if (command === "selectall") {
  await evaluate(`(() => {
    const target = document.querySelector('textarea[aria-label^="Editor content"]');
    target?.focus();
    return Boolean(target);
  })()`);
  await send("Input.dispatchKeyEvent", {
    type: "rawKeyDown",
    key: "a",
    code: "KeyA",
    windowsVirtualKeyCode: 65,
    nativeVirtualKeyCode: 65,
    modifiers: 2,
    commands: ["selectAll"],
  });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: "a", code: "KeyA", windowsVirtualKeyCode: 65, modifiers: 2 });
} else if (command === "cleareditor") {
  await evaluate(`(() => {
    const target = document.querySelector('textarea[aria-label^="Editor content"]');
    target?.focus();
    return Boolean(target);
  })()`);
  await send("Input.dispatchKeyEvent", {
    type: "rawKeyDown",
    key: "a",
    code: "KeyA",
    windowsVirtualKeyCode: 65,
    nativeVirtualKeyCode: 65,
    modifiers: 2,
    commands: ["selectAll"],
  });
  await send("Input.dispatchKeyEvent", {
    type: "rawKeyDown",
    key: "Backspace",
    code: "Backspace",
    windowsVirtualKeyCode: 8,
    nativeVirtualKeyCode: 8,
    commands: ["deleteBackward"],
  });
  await send("Input.dispatchKeyEvent", {
    type: "keyUp",
    key: "Backspace",
    code: "Backspace",
    windowsVirtualKeyCode: 8,
  });
  console.log(JSON.stringify({ ok: true }));
} else if (command === "eval") {
  console.log(JSON.stringify(await evaluate(args.join(" "))));
} else if (command === "tabledata") {
  const output = args[0];
  const maxScroll = Number(args[1] || 0);
  const rows = await evaluate(`(async () => {
    const container = Array.from(document.querySelectorAll('*')).find((element) =>
      element.scrollHeight > element.clientHeight + 20 &&
      element.clientHeight > 100 &&
      element.querySelectorAll('tr').length > 5
    );
    if (!container) throw new Error('TradingView table container not found.');

    const records = new Map();
    const collect = () => {
      for (const row of container.querySelectorAll('tr')) {
        const cells = Array.from(row.querySelectorAll('td'));
        if (cells.length < 7) continue;
        const values = cells.slice(0, 7).map((cell) =>
          cell.getAttribute('data-copy-value') || (cell.innerText || '').trim()
        );
        if (!values[0]) continue;
        records.set(values[0], {
          date: values[0],
          open: values[1],
          high: values[2],
          low: values[3],
          close: values[4],
          change: values[5],
          volume: values[6],
        });
      }
    };

    const step = Math.max(240, Math.floor(container.clientHeight * 0.8));
    const scrollLimit = ${maxScroll} > 0 ? Math.min(container.scrollHeight, ${maxScroll}) : container.scrollHeight;
    for (let top = 0; top <= scrollLimit; top += step) {
      container.scrollTop = top;
      container.dispatchEvent(new Event('scroll', { bubbles: true }));
      await new Promise((resolve) => setTimeout(resolve, 45));
      collect();
    }
    container.scrollTop = 0;
    container.dispatchEvent(new Event('scroll', { bubbles: true }));
    return Array.from(records.values());
  })()`);
  fs.writeFileSync(output, JSON.stringify(rows, null, 2));
  console.log(JSON.stringify({ output, rows: rows.length }));
} else if (command === "candles") {
  const [output, requestedSymbol, requestedResolution, requestedSession = "regular"] = args;
  const requestedBars = Math.max(1, Number(args[4] || 10000));
  if (!output || !requestedSymbol || !requestedResolution) {
    throw new Error(
      "Usage: tv_cdp.mjs candles OUTPUT SYMBOL RESOLUTION [SESSION] [BAR_COUNT]"
    );
  }

  const chart = await evaluate(`(() => {
    const watched = window._exposed_chartWidgetCollection?.activeChartWidget;
    return Boolean(watched?.value?.());
  })()`);
  if (!chart) throw new Error("TradingView active chart widget is unavailable.");

  const extraction = await evaluate(`(async () => {
    const chart = window._exposed_chartWidgetCollection.activeChartWidget.value();
    const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
    const waitForSeries = async () => {
      let previousSize = -1;
      let stableChecks = 0;
      for (let attempt = 0; attempt < 160; attempt++) {
        await sleep(250);
        const series = chart.model().mainSeries();
        const size = series.data().size();
        stableChecks = size === previousSize ? stableChecks + 1 : 0;
        previousSize = size;
        if (stableChecks >= 8 && !series.isLoading()) return;
      }
      throw new Error('TradingView series did not finish loading.');
    };

    chart.setSymbol(${JSON.stringify(requestedSymbol)});
    await waitForSeries();
    chart.setResolution(${JSON.stringify(requestedResolution)});
    await waitForSeries();

    let series = chart.model().mainSeries();
    const sessionProperty = series.properties().childs().sessionId;
    if (sessionProperty?.value?.() !== ${JSON.stringify(requestedSession)}) {
      sessionProperty.setValue(${JSON.stringify(requestedSession)});
      await waitForSeries();
      series = chart.model().mainSeries();
    }

    const targetCount = ${requestedBars};
    for (let attempt = 0; attempt < 8 && series.data().size() < targetCount; attempt++) {
      const before = series.data().size();
      if (!series.requestMoreDataAvailable()) break;
      series.requestMoreData(targetCount - before + 250);
      await waitForSeries();
      series = chart.model().mainSeries();
      if (series.data().size() <= before) break;
    }

    const rowsByTime = new Map();
    series.data().each((_index, value) => {
      if (!Array.isArray(value) || value.length < 6 || !Number.isFinite(value[0])) return;
      rowsByTime.set(value[0], {
        date: new Date(value[0] * 1000).toISOString(),
        open: value[1],
        high: value[2],
        low: value[3],
        close: value[4],
        volume: value[5],
      });
    });
    const bars = Array.from(rowsByTime.values()).sort((left, right) =>
      left.date.localeCompare(right.date)
    );
    const info = series.symbolInfo() || {};
    const properties = series.properties().childs();
    return {
      metadata: {
        source_mode: 'tradingview_cdp_snapshot',
        captured_at: new Date().toISOString(),
        requested_symbol: ${JSON.stringify(requestedSymbol)},
        chart_symbol: chart.getSymbol(),
        provider_symbol: series.symbolParams()?.symbol || null,
        resolved_symbol: info.full_name || null,
        exchange: info.exchange || null,
        listed_exchange: info.listed_exchange || null,
        resolution: chart.getResolution(),
        session: series.sessionId(),
        session_hours: info.session || null,
        chart_timezone: chart.getTimezone(),
        exchange_timezone: info.timezone || null,
        dividends_adjusted: properties.dividendsAdjustment?.value?.() ?? null,
        volume_scope: info.exchange
          ? info.exchange + ' intraday feed; compare volume only within the same feed and timeframe'
          : 'unknown',
        requested_bar_count: targetCount,
        returned_bar_count: bars.length,
        more_data_available: series.requestMoreDataAvailable(),
      },
      bars,
    };
  })()`);

  fs.writeFileSync(output, JSON.stringify(extraction, null, 2));
  console.log(JSON.stringify({ output, metadata: extraction.metadata }));
} else {
  throw new Error(`Unknown command: ${command}`);
}

socket.close();
