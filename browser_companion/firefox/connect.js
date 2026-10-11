(async () => {
    if (!document.querySelector('meta[name="abyssbeacon-red-bridge"][content="1"]')) return;
    const status = document.getElementById("bridge-status");
    try {
        await browser.runtime.sendMessage({type: "connect-abyssbeacon"});
        if (status) status.textContent = "Connected. Opening the selected file in your signed-in Firefox session…";
    } catch (_) {
        if (status) status.textContent = "Could not connect to AbyssBeacon. Keep it running, retry the download, and use the new authorization tab.";
    }
})();
