(() => {
  if (window.__eyentraCaptureLoaded) return;
  window.__eyentraCaptureLoaded = true;

  const API_BASE = "http://127.0.0.1:5000";
  let overlay;
  let selection;
  let startX = 0;
  let startY = 0;

  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === "EYENTRA_START_SELECTION") {
      startSelection();
    }
  });

  function startSelection() {
    removeOverlay();

    overlay = document.createElement("div");
    overlay.className = "eyentra-overlay";
    overlay.innerHTML = `
      <div class="eyentra-toolbar">Drag to capture a screen section for Eyentra</div>
      <div class="eyentra-selection"></div>
      <div class="eyentra-toast" hidden></div>
    `;
    document.documentElement.appendChild(overlay);

    selection = overlay.querySelector(".eyentra-selection");
    injectStyle();

    overlay.addEventListener("mousedown", onMouseDown);
    overlay.addEventListener("mousemove", onMouseMove);
    overlay.addEventListener("mouseup", onMouseUp);
    window.addEventListener("keydown", onKeyDown);
  }

  function onMouseDown(event) {
    if (event.button !== 0) return;
    startX = event.clientX;
    startY = event.clientY;
    drawSelection(startX, startY, 0, 0);
    selection.dataset.dragging = "true";
  }

  function onMouseMove(event) {
    if (selection.dataset.dragging !== "true") return;
    const x = Math.min(event.clientX, startX);
    const y = Math.min(event.clientY, startY);
    const width = Math.abs(event.clientX - startX);
    const height = Math.abs(event.clientY - startY);
    drawSelection(x, y, width, height);
  }

  async function onMouseUp() {
    if (selection.dataset.dragging !== "true") return;
    selection.dataset.dragging = "false";

    const rect = selection.getBoundingClientRect();
    if (rect.width < 12 || rect.height < 12) {
      showToast("Selection is too small.");
      return;
    }

    showToast("Uploading capture...");
    const response = await chrome.runtime.sendMessage({ type: "EYENTRA_CAPTURE_VISIBLE_TAB" });
    if (!response || response.error) {
      showToast(response?.error || "Could not capture this tab.");
      return;
    }

    try {
      const cropped = await cropDataUrl(response.dataUrl, rect);
      const upload = await fetch(`${API_BASE}/api/extension/screenshot`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          image: cropped,
          filename: `eyentra_capture_${Date.now()}.png`
        })
      });

      const result = await upload.json();
      if (!upload.ok) {
        showToast(result.error || "Login to Eyentra first.");
        return;
      }

      showToast("Capture uploaded. Opening QR details...");
      window.setTimeout(() => {
        removeOverlay();
        window.open(result.detail_url, "_blank");
      }, 700);
    } catch (error) {
      showToast("Upload failed. Is Eyentra running?");
    }
  }

  function cropDataUrl(dataUrl, rect) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => {
        const scaleX = image.width / window.innerWidth;
        const scaleY = image.height / window.innerHeight;
        const canvas = document.createElement("canvas");
        canvas.width = Math.round(rect.width * scaleX);
        canvas.height = Math.round(rect.height * scaleY);
        const context = canvas.getContext("2d");
        context.drawImage(
          image,
          Math.round(rect.left * scaleX),
          Math.round(rect.top * scaleY),
          canvas.width,
          canvas.height,
          0,
          0,
          canvas.width,
          canvas.height
        );
        resolve(canvas.toDataURL("image/png"));
      };
      image.onerror = reject;
      image.src = dataUrl;
    });
  }

  function drawSelection(x, y, width, height) {
    selection.style.left = `${x}px`;
    selection.style.top = `${y}px`;
    selection.style.width = `${width}px`;
    selection.style.height = `${height}px`;
  }

  function onKeyDown(event) {
    if (event.key === "Escape") removeOverlay();
  }

  function showToast(message) {
    const toast = overlay.querySelector(".eyentra-toast");
    toast.textContent = message;
    toast.hidden = false;
  }

  function removeOverlay() {
    document.querySelectorAll(".eyentra-overlay").forEach((node) => node.remove());
    window.removeEventListener("keydown", onKeyDown);
  }

  function injectStyle() {
    if (document.getElementById("eyentra-capture-style")) return;
    const style = document.createElement("style");
    style.id = "eyentra-capture-style";
    style.textContent = `
      .eyentra-overlay {
        position: fixed;
        inset: 0;
        z-index: 2147483647;
        cursor: crosshair;
        background: rgba(8, 18, 16, 0.24);
        font-family: Inter, Arial, sans-serif;
      }
      .eyentra-toolbar,
      .eyentra-toast {
        position: fixed;
        left: 50%;
        transform: translateX(-50%);
        padding: 10px 14px;
        color: #fff;
        background: #17211d;
        border: 1px solid rgba(255,255,255,0.2);
        border-radius: 8px;
        box-shadow: 0 18px 40px rgba(0,0,0,0.28);
        font-size: 14px;
        font-weight: 700;
      }
      .eyentra-toolbar { top: 18px; }
      .eyentra-toast { bottom: 18px; }
      .eyentra-selection {
        position: fixed;
        border: 2px solid #2fbf9b;
        background: rgba(47, 191, 155, 0.12);
        box-shadow: 0 0 0 9999px rgba(8, 18, 16, 0.34);
        width: 0;
        height: 0;
      }
    `;
    document.documentElement.appendChild(style);
  }
})();
