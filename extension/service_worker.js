const EYENTRA_UPLOAD_URL = "http://127.0.0.1:5000/api/extension/upload";

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id) return;

  try {
    await chrome.tabs.sendMessage(tab.id, { type: "EYENTRA_START_CAPTURE" });
  } catch (error) {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["content.js"]
    });
    await chrome.tabs.sendMessage(tab.id, { type: "EYENTRA_START_CAPTURE" });
  }
});

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message.type !== "EYENTRA_CAPTURE_RECT") return;
  captureCropAndUpload(message.rect, sender.tab);
});

async function captureCropAndUpload(rect, tab) {
  try {
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    const blob = await cropDataUrl(dataUrl, rect);
    const formData = new FormData();
    formData.append("capture", blob, "eyentra-capture.png");

    const response = await fetch(EYENTRA_UPLOAD_URL, {
      method: "POST",
      credentials: "include",
      body: formData
    });
    const payload = await response.json();

    if (!response.ok || !payload.ok) {
      notify("Eyentra upload failed", payload.error || "Open Eyentra and log in first.");
      if (payload.login_url) chrome.tabs.create({ url: payload.login_url });
      return;
    }

    notify("Eyentra capture uploaded", "QR code and password are ready.");
    chrome.tabs.create({ url: payload.detail_url });
  } catch (error) {
    notify("Eyentra capture failed", error.message || "Could not capture this page.");
  }
}

async function cropDataUrl(dataUrl, rect) {
  const sourceBlob = await (await fetch(dataUrl)).blob();
  const bitmap = await createImageBitmap(sourceBlob);
  const scale = rect.devicePixelRatio || 1;
  const sx = Math.max(0, Math.round(rect.x * scale));
  const sy = Math.max(0, Math.round(rect.y * scale));
  const sw = Math.max(1, Math.round(rect.width * scale));
  const sh = Math.max(1, Math.round(rect.height * scale));

  const canvas = new OffscreenCanvas(sw, sh);
  const context = canvas.getContext("2d");
  context.drawImage(bitmap, sx, sy, sw, sh, 0, 0, sw, sh);
  return canvas.convertToBlob({ type: "image/png" });
}

function notify(title, message) {
  chrome.action.setTitle({ title: `${title}: ${message}` });
  console.log(`[Eyentra] ${title}: ${message}`);
}
