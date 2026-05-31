(function () {
  if (window.__eyentraCaptureLoaded) return;
  window.__eyentraCaptureLoaded = true;

  const EYENTRA_OVERLAY_ID = "eyentra-capture-overlay";

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type === "EYENTRA_START_CAPTURE") {
      startEyentraCapture();
      sendResponse({ ok: true });
    }
  });

  function startEyentraCapture() {
  const oldOverlay = document.getElementById(EYENTRA_OVERLAY_ID);
  if (oldOverlay) oldOverlay.remove();

  const overlay = document.createElement("div");
  overlay.id = EYENTRA_OVERLAY_ID;
  overlay.innerHTML = `
    <div class="eyentra-crosshair"></div>
    <div class="eyentra-selection"></div>
    <div class="eyentra-toast">Drag over the section to upload to Eyentra</div>
  `;
  document.documentElement.appendChild(overlay);

  const selection = overlay.querySelector(".eyentra-selection");
  let startX = 0;
  let startY = 0;
  let dragging = false;

  overlay.addEventListener("mousedown", (event) => {
    dragging = true;
    startX = event.clientX;
    startY = event.clientY;
    drawSelection(selection, startX, startY, 0, 0);
  });

  overlay.addEventListener("mousemove", (event) => {
    if (!dragging) return;
    const left = Math.min(startX, event.clientX);
    const top = Math.min(startY, event.clientY);
    const width = Math.abs(event.clientX - startX);
    const height = Math.abs(event.clientY - startY);
    drawSelection(selection, left, top, width, height);
  });

  overlay.addEventListener("mouseup", (event) => {
    if (!dragging) return;
    dragging = false;
    const rect = {
      x: Math.min(startX, event.clientX),
      y: Math.min(startY, event.clientY),
      width: Math.abs(event.clientX - startX),
      height: Math.abs(event.clientY - startY),
      devicePixelRatio: window.devicePixelRatio || 1
    };

    if (rect.width < 12 || rect.height < 12) {
      overlay.remove();
      return;
    }

    overlay.querySelector(".eyentra-toast").textContent = "Uploading capture to Eyentra...";
    chrome.runtime.sendMessage({ type: "EYENTRA_CAPTURE_RECT", rect });
    setTimeout(() => overlay.remove(), 500);
  });

  overlay.addEventListener("keydown", (event) => {
    if (event.key === "Escape") overlay.remove();
  });
  overlay.tabIndex = 0;
  overlay.focus();
  }

  function drawSelection(element, left, top, width, height) {
    element.style.left = `${left}px`;
    element.style.top = `${top}px`;
    element.style.width = `${width}px`;
    element.style.height = `${height}px`;
  }

  const style = document.createElement("style");
  style.textContent = `
  #${EYENTRA_OVERLAY_ID} {
    position: fixed;
    inset: 0;
    z-index: 2147483647;
    cursor: crosshair;
    background: rgba(4, 12, 20, 0.28);
    backdrop-filter: saturate(130%);
  }
  #${EYENTRA_OVERLAY_ID} .eyentra-selection {
    position: fixed;
    border: 2px solid #32f5c8;
    background: rgba(50, 245, 200, 0.14);
    box-shadow: 0 0 0 9999px rgba(4, 12, 20, 0.36), 0 0 28px rgba(50, 245, 200, 0.45);
  }
  #${EYENTRA_OVERLAY_ID} .eyentra-toast {
    position: fixed;
    left: 50%;
    top: 22px;
    transform: translateX(-50%);
    padding: 10px 14px;
    border-radius: 999px;
    background: #07111f;
    color: white;
    font: 600 13px system-ui, -apple-system, Segoe UI, sans-serif;
    box-shadow: 0 14px 45px rgba(0,0,0,0.24);
  }
`;
  document.documentElement.appendChild(style);
})();
