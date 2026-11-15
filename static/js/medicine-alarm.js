(() => {
    "use strict";

    let activeAlarm = null;
    let audioContext = null;
    let alarmInterval = null;
    let pollInterval = null;
    let audioEnabled = false;

    const snoozeTimers = new Map();
    const dismissed = new Set();

    function getTodayKey() {
        const now = new Date();
        const year = now.getFullYear();
        const month = String(now.getMonth() + 1).padStart(2, "0");
        const day = String(now.getDate()).padStart(2, "0");

        return `${year}-${month}-${day}`;
    }

    function alarmKey(alarm) {
        return `${getTodayKey()}-${alarm.schedule_id}-${alarm.scheduled_time}`;
    }

    function alreadyTaken(alarm) {
        try {
            return localStorage.getItem(
                `medicare-taken-${alarmKey(alarm)}`
            ) === "yes";
        } catch {
            return false;
        }
    }

    function markTakenLocally(alarm) {
        try {
            localStorage.setItem(
                `medicare-taken-${alarmKey(alarm)}`,
                "yes"
            );
        } catch {
            // Continue if browser storage is unavailable.
        }
    }

    function createAlarmUI() {
        if (document.getElementById("medicine-alarm-modal")) {
            return;
        }

        const modal = document.createElement("div");
        modal.id = "medicine-alarm-modal";

        Object.assign(modal.style, {
            position: "fixed",
            inset: "0",
            background: "rgba(0, 0, 0, 0.65)",
            display: "none",
            alignItems: "center",
            justifyContent: "center",
            zIndex: "99999",
            padding: "20px"
        });

        modal.innerHTML = `
            <div style="
                background:white;
                color:#212529;
                width:100%;
                max-width:420px;
                padding:28px;
                border-radius:18px;
                text-align:center;
                box-shadow:0 10px 40px rgba(0,0,0,.3);
            ">
                <div style="font-size:48px">💊</div>

                <h2 style="margin:10px 0">Medicine Time!</h2>

                <h3 id="medicine-alarm-name"
                    style="color:#0d6efd"></h3>

                <p id="medicine-alarm-dosage"
                   style="font-size:18px"></p>

                <p id="medicine-alarm-time"></p>

                <p id="medicine-alarm-instructions"
                   style="color:#6c757d"></p>

                <div style="
                    display:flex;
                    flex-wrap:wrap;
                    justify-content:center;
                    gap:10px;
                    margin-top:20px;
                ">
                    <button id="medicine-alarm-taken"
                        type="button"
                        style="
                            background:#198754;color:white;
                            border:0;border-radius:8px;
                            padding:12px 18px;cursor:pointer;
                        ">
                        ✓ Taken
                    </button>

                    <button id="medicine-alarm-snooze"
                        type="button"
                        style="
                            background:#ffc107;color:#212529;
                            border:0;border-radius:8px;
                            padding:12px 18px;cursor:pointer;
                        ">
                        ⏰ Snooze 5 min
                    </button>

                    <button id="medicine-alarm-stop"
                        type="button"
                        style="
                            background:#dc3545;color:white;
                            border:0;border-radius:8px;
                            padding:12px 18px;cursor:pointer;
                        ">
                        🔕 Stop Alarm
                    </button>
                </div>

                <p id="medicine-alarm-message"
                   role="status"
                   style="margin-top:15px"></p>
            </div>
        `;

        document.body.appendChild(modal);

        document.getElementById("medicine-alarm-taken")
            .addEventListener("click", () => respondToAlarm("taken"));

        document.getElementById("medicine-alarm-snooze")
            .addEventListener("click", () => respondToAlarm("snoozed"));

        document.getElementById("medicine-alarm-stop")
            .addEventListener("click", stopAlarm);
    }

    async function enableAlarmAudio() {
        try {
            const AudioContextClass =
                window.AudioContext || window.webkitAudioContext;

            if (!AudioContextClass) {
                return;
            }

            if (!audioContext) {
                audioContext = new AudioContextClass();
            }

            if (audioContext.state === "suspended") {
                await audioContext.resume();
            }

            audioEnabled = audioContext.state === "running";
        } catch (error) {
            console.warn("Alarm audio could not start:", error);
        }
    }

    function playBeep() {
        if (!audioEnabled || !audioContext) {
            return;
        }

        try {
            const oscillator = audioContext.createOscillator();
            const gain = audioContext.createGain();

            oscillator.type = "sine";
            oscillator.frequency.value = 880;

            gain.gain.setValueAtTime(
                0.001,
                audioContext.currentTime
            );

            gain.gain.exponentialRampToValueAtTime(
                0.2,
                audioContext.currentTime + 0.03
            );

            gain.gain.exponentialRampToValueAtTime(
                0.001,
                audioContext.currentTime + 0.4
            );

            oscillator.connect(gain);
            gain.connect(audioContext.destination);

            oscillator.start();
            oscillator.stop(audioContext.currentTime + 0.42);
        } catch (error) {
            console.warn("Could not play alarm:", error);
        }
    }

    function stopSound() {
        if (alarmInterval) {
            clearInterval(alarmInterval);
            alarmInterval = null;
        }
    }

    function stopAlarm() {
        stopSound();

        const modal = document.getElementById("medicine-alarm-modal");

        if (modal) {
            modal.style.display = "none";
        }

        if (activeAlarm) {
            dismissed.add(alarmKey(activeAlarm));
        }

        activeAlarm = null;
    }

    function showAlarm(alarm) {
        if (activeAlarm || dismissed.has(alarmKey(alarm))) {
            return;
        }

        activeAlarm = alarm;
        createAlarmUI();

        document.getElementById("medicine-alarm-name").textContent =
            alarm.medicine_name;

        document.getElementById("medicine-alarm-dosage").textContent =
            `Dosage: ${alarm.dosage}`;

        document.getElementById("medicine-alarm-time").textContent =
            `Scheduled time: ${alarm.formatted_time}`;

        document.getElementById("medicine-alarm-instructions").textContent =
            alarm.instructions;

        document.getElementById("medicine-alarm-message").textContent = "";

        document.getElementById("medicine-alarm-modal").style.display =
            "flex";

        playBeep();

        if (audioEnabled) {
            alarmInterval = setInterval(playBeep, 1800);
        }

        if ("Notification" in window &&
            Notification.permission === "granted") {
            try {
                new Notification("💊 Medicine Time!", {
                    body: `${alarm.medicine_name} — ${alarm.dosage}`,
                    tag: `medicare-${alarmKey(alarm)}`
                });
            } catch (error) {
                console.warn("Browser notification unavailable:", error);
            }
        }
    }

    async function respondToAlarm(status) {
        if (!activeAlarm) {
            return;
        }

        const alarm = activeAlarm;
        const message = document.getElementById(
            "medicine-alarm-message"
        );

        const buttons = [
            document.getElementById("medicine-alarm-taken"),
            document.getElementById("medicine-alarm-snooze"),
            document.getElementById("medicine-alarm-stop")
        ];

        buttons.forEach(button => button.disabled = true);

        try {
            const response = await fetch(
                `/api/medicine-alarms/${alarm.schedule_id}/response`,
                {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify({ status })
                }
            );

            const result = await response.json();

            if (!response.ok || !result.success) {
                throw new Error(
                    result.error || "Could not save your response."
                );
            }

            stopSound();

            if (status === "taken") {
                markTakenLocally(alarm);
                dismissed.add(alarmKey(alarm));
                message.textContent = "Dose marked as taken.";
                activeAlarm = null;

                setTimeout(() => {
                    const modal = document.getElementById(
                        "medicine-alarm-modal"
                    );

                    if (modal) {
                        modal.style.display = "none";
                    }

                    window.location.reload();
                }, 900);

                return;
            }

            // Snoozing schedules another browser alarm after five minutes.
            dismissed.add(alarmKey(alarm));

            const snoozedAlarm = { ...alarm };
            activeAlarm = null;

            message.textContent = "Alarm snoozed for 5 minutes.";

            setTimeout(() => {
                const modal = document.getElementById(
                    "medicine-alarm-modal"
                );

                if (modal) {
                    modal.style.display = "none";
                }
            }, 700);

            const timer = setTimeout(() => {
                snoozeTimers.delete(alarmKey(snoozedAlarm));

                dismissed.delete(alarmKey(snoozedAlarm));

                // Reopen only if the dose has not been marked taken.
                if (!alreadyTaken(snoozedAlarm)) {
                    showAlarm(snoozedAlarm);
                }
            }, 5 * 60 * 1000);

            snoozeTimers.set(alarmKey(snoozedAlarm), timer);

        } catch (error) {
            message.textContent = error.message;
        } finally {
            buttons.forEach(button => button.disabled = false);
        }
    }

    async function checkMedicineAlarms() {
        if (activeAlarm) {
            return;
        }

        try {
            const response = await fetch("/api/medicine-alarms", {
                headers: { "Accept": "application/json" },
                cache: "no-store"
            });

            if (!response.ok) {
                return;
            }

            const data = await response.json();

            if (!data.success || !Array.isArray(data.alarms)) {
                return;
            }

            const dueAlarm = data.alarms.find(alarm =>
                !alreadyTaken(alarm) &&
                !dismissed.has(alarmKey(alarm))
            );

            if (dueAlarm) {
                showAlarm(dueAlarm);
            }
        } catch (error) {
            console.warn("Could not check medicine alarms:", error);
        }
    }

    document.addEventListener("DOMContentLoaded", () => {
        createAlarmUI();

        // Browser audio generally requires an initial user interaction.
        document.addEventListener("click", enableAlarmAudio, {
            once: true
        });

        document.addEventListener("keydown", enableAlarmAudio, {
            once: true
        });

        checkMedicineAlarms();
        pollInterval = setInterval(checkMedicineAlarms, 15000);
    });
})();