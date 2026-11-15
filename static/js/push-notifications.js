function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - base64String.length % 4) % 4);

    const base64 = (base64String + padding)
        .replace(/-/g, "+")
        .replace(/_/g, "/");

    const rawData = window.atob(base64);

    return Uint8Array.from(
        [...rawData].map(char => char.charCodeAt(0))
    );
}

async function enableMedicineNotifications() {
    try {
        // Check browser support
        if (
            !("serviceWorker" in navigator) ||
            !("PushManager" in window) ||
            !("Notification" in window)
        ) {
            alert("Your browser does not support push notifications.");
            return;
        }

        // Ask permission
        const permission = await Notification.requestPermission();

        if (permission !== "granted") {
            alert("Please allow notifications in your browser.");
            return;
        }

        // Register service worker
        const registration = await navigator.serviceWorker.register(
            "/static/service-worker.js"
        );

        await navigator.serviceWorker.ready;

        // Get VAPID public key from Flask backend
        const keyResponse = await fetch("/push/public-key");

        if (!keyResponse.ok) {
            throw new Error("Could not get VAPID public key from server.");
        }

        const keyData = await keyResponse.json();

        // Create push subscription
        let subscription = await registration.pushManager.getSubscription();

        if (!subscription) {
            subscription = await registration.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: urlBase64ToUint8Array(
                    keyData.publicKey
                )
            });
        }

        // Send subscription to Flask backend
        const response = await fetch("/push/subscribe", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify(subscription)
        });

        if (!response.ok) {
            throw new Error("Could not save notification subscription.");
        }

        alert("Medicine push notifications enabled successfully!");

    } catch (error) {
        console.error("Push notification error:", error);

        alert(
            "Notification setup failed. Check the browser console and Flask server."
        );
    }
}

document.addEventListener("DOMContentLoaded", function () {
    const button = document.getElementById("enable-notifications");

    if (button) {
        button.addEventListener(
            "click",
            enableMedicineNotifications
        );
    }
});