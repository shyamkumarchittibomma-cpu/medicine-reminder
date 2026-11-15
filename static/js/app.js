// ==================================================
// MediCare AI - Smart Medicine Alarm System
// Features: Browser Notifications, Alarm Sound,
// Continuous Alarm, Snooze, Dismiss, Duplicate Fix
// ==================================================

let medicineSchedules = [];
let medicineTimers = [];
let snoozeTimers = [];
let audioContext = null;
let alarmInterval = null;
let alarmIsPlaying = false;
let currentAlarmMedicine = null;
let remindersInitialized = false;

// ------------------------------------------
// 1. Initialize audio after user interaction
// ------------------------------------------

function initializeAudio() {
    try {
        const AudioContextClass =
            window.AudioContext || window.webkitAudioContext;

        if (!AudioContextClass) {
            console.warn("Web Audio API is not supported.");
            return false;
        }

        if (!audioContext) {
            audioContext = new AudioContextClass();
        }

        if (audioContext.state === "suspended") {
            audioContext.resume();
        }

        return true;
    } catch (error) {
        console.error("Audio initialization failed:", error);
        return false;
    }
}

// ------------------------------------------
// 2. Play one short notification sound
// ------------------------------------------

function playNormalSound() {
    if (!initializeAudio()) return;

    if (audioContext.state === "suspended") {
        audioContext.resume();
    }

    const oscillator = audioContext.createOscillator();
    const gain = audioContext.createGain();

    oscillator.type = "sine";
    oscillator.frequency.value = 880;

    gain.gain.setValueAtTime(0.0001, audioContext.currentTime);
    gain.gain.exponentialRampToValueAtTime(
        0.25,
        audioContext.currentTime + 0.04
    );
    gain.gain.exponentialRampToValueAtTime(
        0.0001,
        audioContext.currentTime + 0.45
    );

    oscillator.connect(gain);
    gain.connect(audioContext.destination);

    oscillator.start();
    oscillator.stop(audioContext.currentTime + 0.5);
}

// ------------------------------------------
// 3. Continuous alarm sound
// ------------------------------------------

function playAlarmBeep() {
    if (!audioContext || audioContext.state !== "running") {
        return;
    }

    const oscillator = audioContext.createOscillator();
    const gain = audioContext.createGain();

    oscillator.type = "square";
    oscillator.frequency.value = 880;

    gain.gain.setValueAtTime(0.0001, audioContext.currentTime);
    gain.gain.exponentialRampToValueAtTime(
        0.12,
        audioContext.currentTime + 0.03
    );
    gain.gain.setValueAtTime(
        0.12,
        audioContext.currentTime + 0.2
    );
    gain.gain.exponentialRampToValueAtTime(
        0.0001,
        audioContext.currentTime + 0.28
    );

    oscillator.connect(gain);
    gain.connect(audioContext.destination);

    oscillator.start();
    oscillator.stop(audioContext.currentTime + 0.3);
}

function startContinuousAlarm() {
    stopAlarmSound();

    if (!initializeAudio()) {
        alert("Audio is unavailable. Please check your browser settings.");
        return;
    }

    alarmIsPlaying = true;
    playAlarmBeep();

    alarmInterval = setInterval(function () {
        if (alarmIsPlaying) {
            playAlarmBeep();
        }
    }, 600);
}

function stopAlarmSound() {
    alarmIsPlaying = false;

    if (alarmInterval !== null) {
        clearInterval(alarmInterval);
        alarmInterval = null;
    }
}

// ------------------------------------------
// 4. Notification permission
// ------------------------------------------

async function requestNotificationPermission() {
    if (!("Notification" in window)) {
        alert("Your browser does not support notifications.");
        return false;
    }

    if (Notification.permission === "granted") {
        return true;
    }

    if (Notification.permission === "denied") {
        alert(
            "Notifications are blocked. Allow notifications " +
            "in your browser's site settings."
        );
        return false;
    }

    const permission = await Notification.requestPermission();
    return permission === "granted";
}

// ------------------------------------------
// 5. Show browser notification
// ------------------------------------------

function showMedicineNotification(title, message) {
    if (
        "Notification" in window &&
        Notification.permission === "granted"
    ) {
        try {
            const notification = new Notification(title, {
                body: message,
                tag: "medicare-" + Date.now()
            });

            notification.onclick = function () {
                window.focus();
                notification.close();
            };
        } catch (error) {
            console.error("Notification failed:", error);
        }
    }
}

// ------------------------------------------
// 6. Sound mode settings
// ------------------------------------------

function getSoundMode() {
    return localStorage.getItem("medicareSoundMode") || "alarm";
}

function setSoundMode(mode) {
    localStorage.setItem("medicareSoundMode", mode);

    const label = document.getElementById("medicare-sound-status");

    if (label) {
        label.textContent =
            mode === "alarm"
                ? "Continuous Alarm"
                : "Normal Sound";
    }
}

function createSoundSettings() {
    if (document.getElementById("medicare-sound-settings")) {
        return;
    }

    const enableButton =
        document.getElementById("enable-notifications");

    if (!enableButton) return;

    const container = document.createElement("div");

    container.id = "medicare-sound-settings";
    container.className = "mt-3 p-3 border rounded";
    container.style.maxWidth = "360px";

    const label = document.createElement("label");

    label.htmlFor = "medicare-sound-mode";
    label.className = "form-label fw-bold";
    label.textContent = "⏰ Alarm Sound Mode";

    const select = document.createElement("select");

    select.id = "medicare-sound-mode";
    select.className = "form-select";

    const normalOption = document.createElement("option");
    normalOption.value = "normal";
    normalOption.textContent = "🔔 Normal Notification Sound";

    const alarmOption = document.createElement("option");
    alarmOption.value = "alarm";
    alarmOption.textContent = "🚨 Continuous Alarm";

    select.appendChild(normalOption);
    select.appendChild(alarmOption);

    select.value = getSoundMode();

    const status = document.createElement("small");

    status.id = "medicare-sound-status";
    status.className = "text-secondary d-block mt-2";

    select.addEventListener("change", function () {
        setSoundMode(select.value);
    });

    container.appendChild(label);
    container.appendChild(select);
    container.appendChild(status);

    enableButton.insertAdjacentElement("afterend", container);

    setSoundMode(select.value);
}

// ------------------------------------------
// 7. Stop an existing popup
// ------------------------------------------

function closeMedicinePopup() {
    stopAlarmSound();

    const popup = document.getElementById("medicare-alarm-popup");

    if (popup) {
        popup.remove();
    }

    currentAlarmMedicine = null;
}

// ------------------------------------------
// 8. Snooze reminder
// ------------------------------------------

function snoozeMedicine(medicineName) {
    stopAlarmSound();

    const popup = document.getElementById("medicare-alarm-popup");

    if (popup) {
        popup.remove();
    }

    currentAlarmMedicine = null;

    const timer = setTimeout(function () {
        triggerMedicineAlarm(medicineName);
    }, 5 * 60 * 1000);

    snoozeTimers.push(timer);
}

// ------------------------------------------
// 9. Show alarm popup
// ------------------------------------------

function showMedicineAlarmPopup(medicineName) {
    const oldPopup = document.getElementById("medicare-alarm-popup");

    if (oldPopup) {
        oldPopup.remove();
    }

    const popup = document.createElement("div");

    popup.id = "medicare-alarm-popup";

    popup.style.cssText = `
        position: fixed;
        right: 20px;
        bottom: 20px;
        z-index: 99999;
        width: 340px;
        max-width: calc(100vw - 40px);
        padding: 22px;
        background: white;
        color: #263449;
        border-radius: 16px;
        box-shadow: 0 6px 28px rgba(0,0,0,0.24);
        border-top: 5px solid #0d6efd;
    `;

    const title = document.createElement("h5");
    title.textContent = "💊 Medicine Time!";

    const medicine = document.createElement("h4");
    medicine.textContent = medicineName;

    const message = document.createElement("p");
    message.textContent = "It's time for your scheduled medicine.";

    const snoozeButton = document.createElement("button");

    snoozeButton.type = "button";
    snoozeButton.className = "btn btn-warning btn-sm me-2 mb-2";
    snoozeButton.textContent = "⏰ Snooze 5 Minutes";

    snoozeButton.addEventListener("click", function () {
        snoozeMedicine(medicineName);
    });

    const dismissButton = document.createElement("button");

    dismissButton.type = "button";
    dismissButton.className = "btn btn-secondary btn-sm mb-2";
    dismissButton.textContent = "🔕 Dismiss";

    dismissButton.addEventListener("click", function () {
        closeMedicinePopup();
    });

    popup.appendChild(title);
    popup.appendChild(medicine);
    popup.appendChild(message);
    popup.appendChild(snoozeButton);
    popup.appendChild(dismissButton);

    document.body.appendChild(popup);
}

// ------------------------------------------
// 10. Trigger medicine reminder
// ------------------------------------------

function triggerMedicineAlarm(medicineName) {
    currentAlarmMedicine = medicineName;

    showMedicineNotification(
        "💊 MediCare AI - Medicine Time!",
        "It is time to take " + medicineName + "."
    );

    showMedicineAlarmPopup(medicineName);

    if (getSoundMode() === "alarm") {
        startContinuousAlarm();
    } else {
        playNormalSound();
    }
}

// ------------------------------------------
// 11. Schedule medicine reminders
// ------------------------------------------

function clearScheduledReminders() {
    medicineTimers.forEach(function (timer) {
        clearTimeout(timer);
    });

    medicineTimers = [];
}

function scheduleMedicineReminders() {
    clearScheduledReminders();

    const now = new Date();

    console.log("MediCare AI: Scheduling reminders.");
    console.log("Medicines found:", medicineSchedules);

    medicineSchedules.forEach(function (medicine) {
        if (!medicine.name || !medicine.scheduled_time) {
            console.warn("Missing medicine details:", medicine);
            return;
        }

        const parts = String(medicine.scheduled_time).split(":");

        if (parts.length < 2) {
            console.warn("Invalid time:", medicine.scheduled_time);
            return;
        }

        const hour = Number(parts[0]);
        const minute = Number(parts[1]);

        if (
            !Number.isInteger(hour) ||
            !Number.isInteger(minute) ||
            hour < 0 ||
            hour > 23 ||
            minute < 0 ||
            minute > 59
        ) {
            console.warn("Invalid medicine time:", medicine.scheduled_time);
            return;
        }

        const medicineTime = new Date();

        medicineTime.setHours(hour, minute, 0, 0);

        // Do not trigger a missed time immediately.
        // The next scheduled reminder is tomorrow.
        if (medicineTime <= now) {
            console.log(
                "Today's time has passed; skipped:",
                medicine.name
            );
            return;
        }

        const delay = medicineTime.getTime() - now.getTime();

        const timer = setTimeout(function () {
            triggerMedicineAlarm(medicine.name);
        }, delay);

        medicineTimers.push(timer);

        console.log(
            "Reminder scheduled:",
            medicine.name,
            medicineTime.toLocaleTimeString()
        );
    });
}

// ------------------------------------------
// 12. Test alarm sound
// ------------------------------------------

function testAlarmSound() {
    if (!initializeAudio()) {
        alert("Audio is not supported in this browser.");
        return;
    }

    if (getSoundMode() === "alarm") {
        startContinuousAlarm();

        setTimeout(function () {
            stopAlarmSound();
        }, 3000);
    } else {
        playNormalSound();
    }
}

// ------------------------------------------
// 13. Initialize MediCare AI
// ------------------------------------------

document.addEventListener("DOMContentLoaded", function () {
    const dataElement =
        document.getElementById("medicine-schedule-data");

    if (dataElement) {
        try {
            const parsedData = JSON.parse(dataElement.textContent);

            medicineSchedules = Array.isArray(parsedData)
                ? parsedData
                : [];

        } catch (error) {
            console.error("Failed to read medicine schedules:", error);
            medicineSchedules = [];
        }
    }

    createSoundSettings();

    const enableButton =
        document.getElementById("enable-notifications");

    if (enableButton) {
        enableButton.addEventListener("click", async function () {
            // User interaction allows audio to be initialized.
            initializeAudio();

            const allowed = await requestNotificationPermission();

            if (!allowed) {
                alert(
                    "Please allow browser notifications to use reminders."
                );
                return;
            }

            testAlarmSound();

            if (!remindersInitialized) {
                scheduleMedicineReminders();
                remindersInitialized = true;
            }

            alert(
                "MediCare AI reminders enabled! " +
                "Sound test started. Keep the dashboard open."
            );
        });
    }

    // Schedule only once on page load if permission is already granted.
    if (
        "Notification" in window &&
        Notification.permission === "granted"
    ) {
        scheduleMedicineReminders();
        remindersInitialized = true;
    }

    // Add a manual test button beside the sound selector.
    const settings = document.getElementById("medicare-sound-settings");

    if (settings) {
        const testButton = document.createElement("button");

        testButton.type = "button";
        testButton.className = "btn btn-outline-primary btn-sm mt-3";
        testButton.textContent = "🔊 Test Sound";

        testButton.addEventListener("click", function () {
            initializeAudio();
            testAlarmSound();
        });

        settings.appendChild(testButton);
    }
});