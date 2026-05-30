chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id) return;

  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ["content.js"]
  });

  chrome.tabs.sendMessage(tab.id, { type: "EYENTRA_START_SELECTION" });
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type !== "EYENTRA_CAPTURE_VISIBLE_TAB") return false;

  chrome.tabs.captureVisibleTab(sender.tab.windowId, { format: "png" }, (dataUrl) => {
    if (chrome.runtime.lastError) {
      sendResponse({ error: chrome.runtime.lastError.message });
      return;
    }
    sendResponse({ dataUrl });
  });

  return true;
});
