(function () {
  const CHAT_KEY = "omnisync_chat_history";
  const SETTINGS_KEY = "omnisync_assistant_settings";
  const userFromPage = () => document.querySelector("[data-user-id]")?.dataset.userId || window.OMNISYNC_USER_ID || "";

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, function (char) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char];
    });
  }

  function toast(message, kind) {
    const root = document.getElementById("toast-root");
    if (!root) return;
    const item = document.createElement("div");
    item.className = "toast " + (kind || "");
    item.textContent = message;
    root.appendChild(item);
    setTimeout(function () {
      item.classList.add("animate-toast-out");
      setTimeout(function () { item.remove(); }, 250);
    }, 3600);
  }

  async function api(path, options) {
    const config = Object.assign({ method: "GET", headers: {} }, options || {});
    if (config.body && typeof config.body !== "string") {
      config.headers["Content-Type"] = "application/json";
      config.body = JSON.stringify(config.body);
    }
    const token = document.querySelector('meta[name="csrf-token"]')?.content;
    if (token) config.headers["X-CSRFToken"] = token;
    const response = await fetch(path, config);
    let data = null;
    try { data = await response.json(); } catch (error) { data = null; }
    if (!response.ok) throw new Error((data && data.error) || "Request failed (" + response.status + ")");
    return data;
  }

  const post = (path, body) => api(path, { method: "POST", body: body || {} });

  function setupTheme() {
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.addEventListener("click", function () {
        const next = document.documentElement.classList.contains("dark") ? "light" : "dark";
        document.documentElement.className = next;
        localStorage.setItem("theme", next);
        button.setAttribute("aria-label", next === "dark" ? "Switch to Light Mode" : "Switch to Dark Mode");
      });
    });
  }

  function setupMenus() {
    const mobileButton = document.querySelector("[data-mobile-menu]");
    const mobileNav = document.querySelector("[data-mobile-nav]");
    mobileButton?.addEventListener("click", function () { mobileNav?.classList.toggle("open"); });

    const sidebar = document.querySelector("[data-sidebar]");
    const collapse = document.querySelector("[data-sidebar-collapse]");
    const dashboardMenu = document.querySelector("[data-dashboard-menu]");
    if (sidebar && localStorage.getItem("sidebar-collapsed") === "1") sidebar.classList.add("collapsed");
    collapse?.addEventListener("click", function () {
      sidebar?.classList.toggle("collapsed");
      localStorage.setItem("sidebar-collapsed", sidebar?.classList.contains("collapsed") ? "1" : "0");
    });
    dashboardMenu?.addEventListener("click", function () {
      sidebar?.classList.toggle("open");
    });
  }

  function setupModals() {
    document.querySelectorAll("[data-open-modal]").forEach(function (button) {
      button.addEventListener("click", function () {
        const modal = document.querySelector('[data-modal="' + button.dataset.openModal + '"]');
        modal?.classList.remove("hidden");
        if (button.dataset.tools) {
          const tools = button.dataset.tools === "gmail"
            ? ["gmail_list_messages", "gmail_search_messages", "gmail_get_message", "gmail_send_message", "gmail_create_draft", "gmail_get_thread"]
            : ["whatsapp_get_recent_messages", "whatsapp_get_chat_history", "whatsapp_send_message", "whatsapp_search_chats", "whatsapp_summarize_conversations", "whatsapp_get_contact_details", "whatsapp_list_groups", "whatsapp_get_group_messages", "whatsapp_send_group_messages"];
          const title = document.querySelector("[data-tools-title]");
          const list = document.querySelector("[data-tools-list]");
          if (title) title.textContent = button.dataset.tools.toUpperCase() + " Tools";
          if (list) list.innerHTML = '<div class="tools-list">' + tools.map(function (tool) { return "<span>" + escapeHtml(tool) + "</span>"; }).join("") + "</div>";
        }
      });
    });
    document.querySelectorAll("[data-close-modal]").forEach(function (button) {
      button.addEventListener("click", function () { button.closest(".modal")?.classList.add("hidden"); });
    });
    document.querySelectorAll(".modal").forEach(function (modal) {
      modal.addEventListener("click", function (event) { if (event.target === modal) modal.classList.add("hidden"); });
    });
  }

  function setupWaitlist() {
    document.querySelectorAll("[data-waitlist-form]").forEach(function (form) {
      form.addEventListener("submit", async function (event) {
        event.preventDefault();
        const result = form.parentElement?.querySelector("[data-waitlist-result]");
        if (result) result.textContent = "";
        try {
          const data = await post("/api/waitlist", { email: form.email.value });
          if (result) result.textContent = data.message + " Queue Position: #" + data.waitlistCount;
          form.reset();
        } catch (error) {
          toast(error.message, "error");
        }
      });
    });
  }

  const simulatorData = {
    gmail: { name: "Gmail", from: "sarah.finance@corp.com", time: "10:42 AM", initials: "SF", body: "We need to review and sign the NDA before onboarding the contractors. Can you reply today?", output: "Gmail Assistant Core\n\nAI Executive Summary\n• Approval request for contractor NDA\n• Deadline implied: today\n\nSuggested Draft\nHi Sarah, I will review the NDA today and confirm before the onboarding call.\n\nAction: Auto-Draft Reply" },
    whatsapp: { name: "WhatsApp", from: "Alex (Co-Founder)", time: "02:15 PM", initials: "AX", body: "Can we sync at 4:30 tomorrow? I want to align on the roadmap before the investor update.", output: "WhatsApp Sync Core\n\nSmart Calendar Extraction\n• Proposed meeting: tomorrow 4:30 PM\n• Topic: technical roadmap and investor update\n\nAction: Sync to Google Calendar" },
    telegram: { name: "Telegram", from: "Dev Team Channel (99+ messages)", time: "03:55 PM", initials: "DT", body: "Release build is green. Staging deployment is blocked on the final database migration.", output: "Telegram Agent Core\n\nDev Team Channel Digest\n• Build status: green\n• Blocker: final database migration\n\nAction: Create follow-up with release owner" },
    outlook: { name: "Outlook", from: "calendar@outlook-corp.com", time: "05:00 PM", initials: "OC", body: "A 30 minute overlap was detected between the product review and client sync.", output: "Outlook AI Intelligence\n\nOverlap Detected (30 Mins)\n• Product review conflicts with client sync\n\nActions: Propose Reschedule | Ignore" }
  };

  function setupSimulator() {
    const tabs = document.querySelectorAll("[data-channel]");
    if (!tabs.length) return;
    let active = "gmail";
    let playing = false;

    function render() {
      const item = simulatorData[active];
      const name = document.querySelector("[data-sim-channel-name]");
      const from = document.querySelector("[data-sim-from]");
      const time = document.querySelector("[data-sim-time]");
      const body = document.querySelector("[data-sim-body]");
      const avatar = document.querySelector(".sim-avatar");
      if (name) name.textContent = item.name;
      if (from) from.textContent = item.from;
      if (time) time.textContent = item.time;
      if (body) body.textContent = item.body;
      if (avatar) avatar.textContent = item.initials;
    }

    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        active = tab.dataset.channel;
        tabs.forEach(function (item) { item.classList.toggle("active", item === tab); });
        const output = document.querySelector("[data-sim-output]");
        if (output) output.textContent = "Run the simulator to see extracted actions, summaries, and drafts.";
        const badge = document.querySelector("[data-sim-badge]");
        if (badge) badge.textContent = "AI Engine Standby";
        render();
      });
    });

    document.querySelector("[data-sim-run]")?.addEventListener("click", function () {
      if (playing) return;
      playing = true;
      const output = document.querySelector("[data-sim-output]");
      const status = document.querySelector("[data-sim-status]");
      const badge = document.querySelector("[data-sim-badge]");
      const bar = document.querySelector("[data-sim-progress]");
      if (status) status.textContent = "Analyzing parameters...";
      if (badge) badge.textContent = "Analyzing parameters...";
      if (output) output.textContent = "Analyzing parameters...";
      let progress = 0;
      const timer = setInterval(function () {
        progress += 8;
        if (bar) bar.style.width = Math.min(progress, 100) + "%";
        if (progress >= 100) {
          clearInterval(timer);
          const text = simulatorData[active].output;
          if (output) {
            output.textContent = "";
            let index = 0;
            const typing = setInterval(function () {
              output.textContent += text[index] || "";
              index += 1;
              if (index >= text.length) {
                clearInterval(typing);
                if (status) status.textContent = "Completed";
                if (badge) badge.textContent = "Completed";
                playing = false;
              }
            }, 4);
          } else { playing = false; }
        }
      }, 35);
    });
    render();
  }

  function setupAuth() {
    document.querySelectorAll("[data-auth-tab]").forEach(function (tab) {
      tab.addEventListener("click", function () {
        const panelName = tab.dataset.authTab;
        tab.parentElement.querySelectorAll("[data-auth-tab]").forEach(function (item) { item.classList.toggle("active", item === tab); });
        tab.closest(".auth-card").querySelectorAll("[data-auth-panel]").forEach(function (panel) {
          panel.classList.toggle("hidden", panel.dataset.authPanel !== panelName);
        });
      });
    });

    document.querySelectorAll("[data-phone-send-form]").forEach(function (form) {
      form.addEventListener("submit", async function (event) {
        event.preventDefault();
        const panel = form.closest("[data-auth-panel]");
        const errorBox = document.querySelector("[data-auth-error]");
        const label = form.querySelector("[data-otp-send-label]");
        const method = form.querySelector('input[name="method"]:checked')?.value || "sms";
        if (label) label.textContent = "Send OTP via " + (method === "whatsapp" ? "WhatsApp" : "SMS");
        if (errorBox) errorBox.textContent = "";
        try {
          const data = await post("/api/phone-auth/send-otp", { phoneNumber: form.phoneNumber.value, method: method });
          form.dataset.phone = form.phoneNumber.value;
          form.dataset.method = method;
          form.classList.add("hidden");
          const verify = panel?.querySelector("[data-phone-verify-form]");
          verify?.classList.remove("hidden");
          const devOtp = panel?.querySelector("[data-dev-otp]");
          if (devOtp && data.otp) {
            devOtp.textContent = "Development OTP: " + data.otp;
            devOtp.classList.remove("hidden");
          }
          toast(data.message || "OTP sent.");
        } catch (error) {
          if (errorBox) errorBox.textContent = error.message;
        }
      });
    });

    document.querySelectorAll("[data-phone-verify-form]").forEach(function (form) {
      form.addEventListener("submit", async function (event) {
        event.preventDefault();
        const panel = form.closest("[data-auth-panel]");
        const sendForm = panel?.querySelector("[data-phone-send-form]");
        const errorBox = document.querySelector("[data-auth-error]");
        const otp = form.otp.value.trim();
        if (!/^\d{6}$/.test(otp)) {
          if (errorBox) errorBox.textContent = "Please enter the 6-digit OTP code.";
          return;
        }
        try {
          await post("/api/phone-auth/verify-otp", {
            phoneNumber: sendForm?.dataset.phone || sendForm?.phoneNumber.value,
            method: sendForm?.dataset.method || panel?.querySelector('input[name="method"]:checked')?.value,
            otp: otp,
            name: panel?.querySelector('input[name="displayName"]')?.value || ""
          });
          window.location.assign("/");
        } catch (error) {
          if (errorBox) errorBox.textContent = error.message;
        }
      });
    });

    document.querySelectorAll("[data-phone-change]").forEach(function (button) {
      button.addEventListener("click", function () {
        const panel = button.closest("[data-auth-panel]");
        panel?.querySelector("[data-phone-verify-form]")?.classList.add("hidden");
        panel?.querySelector("[data-phone-send-form]")?.classList.remove("hidden");
      });
    });
  }

  function loadChatLocal() {
    try {
      const raw = localStorage.getItem(CHAT_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (Date.now() - parsed.updatedAt > 24 * 60 * 60 * 1000) return null;
      return parsed.messages;
    } catch (error) { return null; }
  }

  function saveChatLocal(messages) {
    localStorage.setItem(CHAT_KEY, JSON.stringify({ updatedAt: Date.now(), messages: messages }));
  }

  function setupChat() {
    const panel = document.querySelector(".chat-panel");
    if (!panel) return;
    const container = panel.querySelector("[data-chat-messages]");
    const status = panel.querySelector("[data-chat-status]");
    const userId = panel.dataset.userId;
    let messages = loadChatLocal() || Array.from(container.querySelectorAll(".chat-bubble")).map(function (bubble) {
      return { sender: bubble.classList.contains("user") ? "user" : "agent", text: bubble.querySelector("p")?.textContent || "" };
    });

    function render() {
      if (!container) return;
      container.innerHTML = messages.map(function (message) {
        const who = message.sender === "user" ? "You" : "OmniSync";
        return '<div class="chat-bubble ' + escapeHtml(message.sender) + '"><b>' + who + '</b><p>' + escapeHtml(message.text) + '</p></div>';
      }).join("");
      container.scrollTop = container.scrollHeight;
    }

    panel.querySelectorAll("[data-chat-suggestion]").forEach(function (button) {
      button.addEventListener("click", function () {
        const input = panel.querySelector('input[name="prompt"]');
        if (input) { input.value = button.textContent.trim(); input.focus(); }
      });
    });

    panel.querySelector("[data-chat-form]")?.addEventListener("submit", async function (event) {
      event.preventDefault();
      const input = event.target.querySelector('input[name="prompt"]');
      const prompt = input.value.trim();
      if (!prompt) return;
      input.value = "";
      messages.push({ sender: "user", text: prompt });
      render();
      if (status) status.textContent = "OmniSync is thinking...";
      try {
        const data = await post("/api/ai-chat", { userId: userId, prompt: prompt, history: messages.slice(-10) });
        const text = data.response || "";
        messages.push({ sender: "agent", text: text });
        saveChatLocal(messages);
        render();
        if (status) status.textContent = "";
      } catch (error) {
        if (status) status.textContent = "Sorry, I encountered an issue querying the Gemini engine. Please try again.";
        toast(error.message, "error");
      }
    });

    panel.querySelector("[data-chat-new]")?.addEventListener("click", async function () {
      messages = [];
      saveChatLocal(messages);
      render();
      localStorage.removeItem(CHAT_KEY);
      try { await post("/api/chat/clear", { userId: userId }); } catch (error) { toast(error.message, "error"); }
    });
    render();
  }

  function setupOverview() {
    document.querySelectorAll("[data-refresh-brief]").forEach(function (button) {
      button.addEventListener("click", async function () {
        const userId = button.closest("[data-user-id]")?.dataset.userId;
        try {
          await post("/api/dashboard-brief", { userId: userId, forceRegenerate: true });
          toast("Brief refreshed.");
          setTimeout(function () { window.location.reload(); }, 500);
        } catch (error) { toast(error.message, "error"); }
      });
    });
    document.querySelectorAll("[data-sync-live]").forEach(function (button) {
      button.addEventListener("click", async function () {
        const userId = userFromPage();
        try {
          await post("/api/dashboard-brief", { userId: userId, forceRegenerate: true });
          await post("/api/alerts/generate", { userId: userId });
          toast("Live sync complete.");
          setTimeout(function () { window.location.reload(); }, 700);
        } catch (error) { toast(error.message, "error"); }
      });
    });
    document.querySelectorAll("[data-brief-action]").forEach(function (button) {
      button.addEventListener("click", function () {
        const target = button.dataset.briefAction.toLowerCase().includes("alert") ? "alerts" : "briefing";
        window.location.assign("/dashboard?tab=" + target);
      });
    });
  }

  function setupBriefing() {
    const panel = document.querySelector("[data-briefing-panel]");
    if (!panel) return;
    const userId = panel.dataset.userId;
    const status = panel.querySelector("[data-briefing-status]");
    panel.querySelector("[data-regenerate-briefing]")?.addEventListener("click", async function () {
      if (status) status.textContent = "Generating...";
      try {
        await post("/api/briefings", { userId: userId });
        toast("Briefing generated.");
        setTimeout(function () { window.location.reload(); }, 500);
      } catch (error) {
        if (status) status.textContent = error.message;
      }
    });
    panel.querySelector("[data-schedule-form]")?.addEventListener("submit", async function (event) {
      event.preventDefault();
      const form = event.target;
      const payload = {
        userId: userId,
        name: form.name.value,
        description: form.description.value,
        apps: Array.from(form.querySelectorAll('input[name="apps"]:checked')).map(function (item) { return item.value; }),
        categories: Array.from(form.querySelectorAll('input[name="categories"]:checked')).map(function (item) { return item.value; }),
        scheduledTime: form.scheduledTime.value,
        frequency: form.frequency.value,
        priorityLevel: form.priorityLevel.value
      };
      if (!payload.apps.length) { toast("Please select at least one connected app.", "error"); return; }
      try {
        await post("/api/briefings/schedules", payload);
        window.location.reload();
      } catch (error) { toast(error.message, "error"); }
    });
    panel.querySelectorAll("[data-delete-schedule]").forEach(function (button) {
      button.addEventListener("click", async function () {
        try {
          await api("/api/briefings/schedules?userId=" + encodeURIComponent(userId) + "&id=" + encodeURIComponent(button.dataset.deleteSchedule), { method: "DELETE" });
          button.closest(".item-row")?.remove();
          toast("Schedule deleted.");
        } catch (error) { toast(error.message, "error"); }
      });
    });
  }

  function mcpPayload(response) {
    try { return JSON.parse(response?.result?.content?.[0]?.text || "{}"); } catch (error) { return {}; }
  }

  function setupIntegrations() {
    const panel = document.querySelector(".integrations-panel");
    if (!panel) return;
    const status = panel.querySelector("[data-integration-status]");
    panel.querySelector("[data-connect-gmail]")?.addEventListener("click", async function () {
      try {
        const data = await api("/api/gmail-connect");
        if (!data.configured || !data.authUrl) {
          document.querySelector('[data-modal="gmail-setup-modal"]')?.classList.remove("hidden");
          return;
        }
        window.location.assign(data.authUrl);
      } catch (error) { toast(error.message, "error"); }
    });

    document.querySelector("[data-gmail-simulate]")?.addEventListener("click", async function () {
      try {
        await post("/api/gmail-connect", { userId: panel.dataset.userId, simulate: true });
        document.querySelector('[data-modal="gmail-setup-modal"]')?.classList.add("hidden");
        window.location.reload();
      } catch (error) { toast(error.message, "error"); }
    });

    panel.querySelectorAll("[data-disconnect]").forEach(function (button) {
      button.addEventListener("click", async function () {
        try {
          await post("/api/integrations/disconnect", { userId: panel.dataset.userId, app: button.dataset.disconnect });
          window.location.reload();
        } catch (error) { toast(error.message, "error"); }
      });
    });

    let whatsappPoll = null;
    let whatsappBusy = false;

    function normalizeWhatsappPhone(value) {
      let digits = String(value || "").replace(/\D/g, "");
      if (digits.startsWith("00")) digits = digits.slice(2);
      digits = digits.replace(/^0+/, "");
      if (digits.length === 10 && /^[6-9]/.test(digits)) digits = "91" + digits;
      if (!/^[1-9]\d{7,14}$/.test(digits)) throw new Error("Enter a valid phone number, with country code if outside India. Indian 10-digit numbers are accepted.");
      return digits;
    }

    function whatsappState(data) {
      if (data?.state) return data.state;
      if (data?.error) return "CONNECTION_FAILED";
      if (data?.connected) return "CONNECTED";
      if (data?.pairingCode) return "PAIRING_CODE_GENERATED";
      return String(data?.status || "DISCONNECTED").toUpperCase();
    }

    function whatsappMessage(data, state) {
      if (data?.error) return data.error;
      if (data?.notice) return data.notice;
      if (data?.message) return data.message;
      if (state === "CONNECTION_FAILED") return "WhatsApp connection failed. Check the error and retry.";
      if (state === "INITIALIZING") return "Initializing WhatsApp connection...";
      if (state === "RECONNECTING") return "WhatsApp connection dropped. Reconnecting...";
      if (state === "PAIRING_CODE_GENERATED") return "Enter the code on your phone. Waiting for WhatsApp...";
      if (state === "CONNECTED") return data?.isSimulated ? "WhatsApp simulated mode enabled. This is not a real WhatsApp account." : "WhatsApp connected successfully!";
      return "WhatsApp disconnected.";
    }

    function applyWhatsappState(data, modal) {
      const state = whatsappState(data);
      const codeBox = modal?.querySelector("[data-wa-code]");
      const copyButton = modal?.querySelector("[data-wa-copy]");
      const waStatus = modal?.querySelector("[data-wa-status]") || status;
      const statePill = modal?.querySelector("[data-wa-state]");
      const accountBox = modal?.querySelector("[data-wa-account]");
      const retryButton = modal?.querySelector('[data-whatsapp-action="retry"]');
      const progress = modal?.querySelector("[data-wa-progress]");
      const phone = data?.phoneNumber || "";

      if (codeBox) {
        codeBox.textContent = data?.pairingCode || "";
        codeBox.classList.toggle("hidden", !data?.pairingCode);
      }
      if (copyButton) copyButton.classList.toggle("hidden", !data?.pairingCode);
      if (statePill) {
        statePill.textContent = state;
        statePill.className = "wa-state" + (state === "CONNECTED" ? " connected" : state === "PAIRING_CODE_GENERATED" ? " pairing" : state === "CONNECTION_FAILED" ? " failed" : "");
      }
      if (retryButton) retryButton.classList.toggle("hidden", state !== "CONNECTION_FAILED");
      if (progress) progress.classList.toggle("hidden", state !== "INITIALIZING" && state !== "RECONNECTING" && !data?.pending);
      if (accountBox) {
        accountBox.textContent = data?.connected ? (data?.isSimulated ? "Simulated account: " + (phone || "demo") + " — not a real WhatsApp connection." : "Connected WhatsApp account: " + phone) : "";
        accountBox.classList.toggle("hidden", !data?.connected);
      }
      let message = whatsappMessage(data, state);
      const minutes = data?.pairingCodeExpiresAt ? Math.max(0, Math.ceil((data.pairingCodeExpiresAt - Date.now()) / 60000)) : 0;
      if (data?.pairingCode && minutes) message += " Code expires in " + minutes + " min.";
      if (waStatus) waStatus.textContent = message;
    }

    function setWhatsappBusy(modal, busy, message) {
      whatsappBusy = busy;
      modal?.querySelectorAll("[data-whatsapp-action]").forEach(function (button) { button.disabled = busy; });
      const progress = modal?.querySelector("[data-wa-progress]");
      if (progress) progress.classList.toggle("hidden", !busy);
      if (busy && message) {
        const waStatus = modal?.querySelector("[data-wa-status]") || status;
        if (waStatus) waStatus.textContent = message;
      }
    }

    function stopWhatsappPoll() {
      if (whatsappPoll) clearInterval(whatsappPoll);
      whatsappPoll = null;
    }

    function startWhatsappPoll(modal) {
      stopWhatsappPoll();
      whatsappPoll = setInterval(async function () {
        try {
          const data = await post("/api/whatsapp-connect", { action: "status", userId: panel.dataset.userId });
          applyWhatsappState(data, modal);
          const state = whatsappState(data);
          if (state === "CONNECTED") {
            stopWhatsappPoll();
            setTimeout(function () { window.location.reload(); }, 1200);
          } else if (state === "CONNECTION_FAILED" || state === "DISCONNECTED") {
            stopWhatsappPoll();
          }
        } catch (error) {
          stopWhatsappPoll();
          applyWhatsappState({ error: error.message, status: "error" }, modal);
        }
      }, 2500);
    }

    const whatsappModal = document.querySelector('[data-modal="whatsapp-modal"]');
    document.querySelectorAll('[data-open-modal="whatsapp-modal"]').forEach(function (button) {
      button.addEventListener("click", function () {
        const codeBox = whatsappModal?.querySelector("[data-wa-code]");
        const copyButton = whatsappModal?.querySelector("[data-wa-copy]");
        const statePill = whatsappModal?.querySelector("[data-wa-state]");
        const accountBox = whatsappModal?.querySelector("[data-wa-account]");
        const waStatus = whatsappModal?.querySelector("[data-wa-status]");
        const retryButton = whatsappModal?.querySelector('[data-whatsapp-action="retry"]');
        if (codeBox) { codeBox.textContent = ""; codeBox.classList.add("hidden"); }
        if (copyButton) copyButton.classList.add("hidden");
        if (statePill) { statePill.textContent = "DISCONNECTED"; statePill.className = "wa-state"; }
        if (accountBox) { accountBox.textContent = ""; accountBox.classList.add("hidden"); }
        if (waStatus) waStatus.textContent = "";
        if (retryButton) retryButton.classList.add("hidden");
      });
    });
    whatsappModal?.querySelector("[data-close-modal]")?.addEventListener("click", stopWhatsappPoll);
    whatsappModal?.querySelector("[data-wa-copy]")?.addEventListener("click", async function () {
      const code = whatsappModal.querySelector("[data-wa-code]")?.textContent?.trim();
      if (!code) return;
      try {
        if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(code);
        else {
          const helper = document.createElement("textarea");
          helper.value = code;
          helper.style.position = "fixed";
          helper.style.opacity = "0";
          document.body.appendChild(helper);
          helper.select();
          document.execCommand("copy");
          helper.remove();
        }
        toast("Pairing code copied.");
      } catch (error) {
        toast("Could not copy the code. Select it manually.", "error");
      }
    });

    panel.querySelectorAll("[data-whatsapp-action]").forEach(function (button) {
      button.addEventListener("click", async function () {
        if (whatsappBusy) return;
        const modal = button.closest(".modal");
        const phoneValue = modal?.querySelector("[data-wa-phone]")?.value || "";
        const requestedAction = button.dataset.whatsappAction;
        const isRealConnect = requestedAction === "connect" || requestedAction === "retry";
        const action = requestedAction === "retry" ? "connect" : requestedAction;
        let payload = { action, userId: panel.dataset.userId };
        if (isRealConnect) {
          try {
            payload.phoneNumber = normalizeWhatsappPhone(phoneValue);
          } catch (error) {
            applyWhatsappState({ error: error.message, status: "error" }, modal);
            toast(error.message, "error");
            return;
          }
        }
        stopWhatsappPoll();
        setWhatsappBusy(modal, true, isRealConnect ? "Initializing WhatsApp connection..." : requestedAction === "status" ? "Checking actual WhatsApp session..." : "Enabling simulated WhatsApp mode...");
        try {
          const data = await post("/api/whatsapp-connect", payload);
          applyWhatsappState(data, modal);
          if (data?.error) toast(data.error, "error");
          else if (data?.notice) toast(data.notice, "success");
          if (data?.connected) {
            stopWhatsappPoll();
            setTimeout(function () { window.location.reload(); }, 1200);
          } else if (isRealConnect && whatsappState(data) !== "CONNECTION_FAILED") {
            startWhatsappPoll(modal);
          }
        } catch (error) {
          applyWhatsappState({ error: error.message, status: "error" }, modal);
          toast(error.message, "error");
        } finally {
          setWhatsappBusy(modal, false);
        }
      });
    });
  }

  let alertRows = [];
  let alertSuggestions = [];

  function renderAlerts(rows) {
    alertRows = rows;
    const list = document.querySelector("[data-alert-list]");
    if (!list) return;
    const high = rows.filter(function (row) { return row.priority === "high"; }).length;
    const resolved = rows.filter(function (row) { return row.status === "resolved"; }).length;
    const active = rows.filter(function (row) { return row.status !== "resolved"; }).length;
    const count = document.querySelector("[data-alert-count]");
    const triggered = document.querySelector("[data-triggered-count]");
    const highCount = document.querySelector("[data-high-count]");
    const resolvedCount = document.querySelector("[data-resolved-count]");
    if (count) count.textContent = active;
    if (triggered) triggered.textContent = rows.length;
    if (highCount) highCount.textContent = high;
    if (resolvedCount) resolvedCount.textContent = resolved;
    if (!rows.length) {
      list.innerHTML = '<p class="empty">No alerts yet. Run an AI scan to find items that need attention.</p>';
      return;
    }
    list.innerHTML = rows.map(function (alert) {
      return '<article class="alert-item" data-alert-row="' + escapeHtml(alert.id) + '">' +
        '<img src="' + escapeHtml(alert.app_logo || "/003-email.png") + '" alt="">' +
        '<div class="alert-body"><b>' + escapeHtml(alert.title) + '</b><p>' + escapeHtml(alert.description) + '</p>' +
        '<small>' + escapeHtml(alert.source_app) + ' · ' + escapeHtml(alert.priority) + ' · ' + escapeHtml(alert.triggered_at) + '</small>' +
        '<div class="alert-actions">' +
        '<button class="btn btn-mini" type="button" data-alert-ai="summary" data-alert-id="' + alert.id + '">AI Summary</button>' +
        '<button class="btn btn-mini" type="button" data-alert-ai="next_action" data-alert-id="' + alert.id + '">AI Suggested Action</button>' +
        '<button class="btn btn-mini" type="button" data-alert-ai="reply" data-alert-id="' + alert.id + '">Draft Reply</button>' +
        '<button class="btn btn-mini" type="button" data-alert-action="snoozed" data-alert-id="' + alert.id + '">Snooze</button>' +
        '<button class="btn btn-mini" type="button" data-alert-action="task" data-alert-id="' + alert.id + '">Task</button>' +
        '<button class="btn btn-mini" type="button" data-alert-action="follow_up" data-alert-id="' + alert.id + '">Follow-up</button>' +
        '<button class="btn btn-mini" type="button" data-alert-action="resolved" data-alert-id="' + alert.id + '">Resolve</button>' +
        '</div><div class="ai-result" data-ai-result="' + alert.id + '"></div></div></article>';
    }).join("");
  }

  function renderSuggestions(rows) {
    alertSuggestions = rows;
    const list = document.querySelector("[data-suggestion-list]");
    if (!list) return;
    if (!rows.length) { list.innerHTML = '<p class="empty">No rule suggestions for these connected apps.</p>'; return; }
    list.innerHTML = rows.map(function (row, index) {
      return '<article class="item-row"><div><b>' + escapeHtml(row.title) + '</b><p>' + escapeHtml(row.description) + '</p><small>' + escapeHtml((row.apps || []).join(", ")) + ' · ' + escapeHtml(row.priority) + '</small></div><button class="btn btn-mini" type="button" data-use-suggestion="' + index + '">Use rule</button></article>';
    }).join("");
  }

  function setupAlerts() {
    const panel = document.querySelector("[data-alerts-panel]");
    if (!panel) return;
    const userId = panel.dataset.userId;
    const message = panel.querySelector("[data-scan-message]");

    async function loadAlerts() {
      try { renderAlerts(await api("/api/alerts?userId=" + encodeURIComponent(userId))); } catch (error) { toast(error.message, "error"); }
    }
    async function loadSuggestions() {
      try { renderSuggestions(await api("/api/alerts/suggestions?userId=" + encodeURIComponent(userId))); } catch (error) { toast(error.message, "error"); }
    }
    loadAlerts();
    loadSuggestions();
    setInterval(loadAlerts, 30000);

    setTimeout(async function () {
      if (alertRows.length) return;
      const connected = document.querySelector(".integrations-panel");
      if (connected && !document.body.classList.contains("has-connected")) return;
      try { await post("/api/alerts/generate", { userId: userId }); await loadAlerts(); } catch (error) { toast(error.message, "error"); }
    }, 600);

    panel.querySelector("[data-alert-scan]")?.addEventListener("click", async function () {
      if (message) message.textContent = "Scanning connected apps with AI...";
      try {
        const data = await post("/api/alerts/generate", { userId: userId });
        if (message) {
          message.textContent = data.created
            ? "AI created " + data.created + " alert(s) from " + data.scanned + " scanned item(s)."
            : (data.scanned ? "AI scanned " + data.scanned + " item(s) and found no new important alerts." : "AI scan found no connected app activity to evaluate.");
        }
        await loadAlerts();
      } catch (error) { if (message) message.textContent = "Unable to run AI scan."; }
    });

    panel.querySelector("[data-alert-rule-form]")?.addEventListener("submit", async function (event) {
      event.preventDefault();
      const form = event.target;
      const apps = Array.from(form.querySelectorAll('input[name="apps"]:checked')).map(function (item) { return item.value; });
      if (!apps.length) { toast("Connect at least one app before creating an alert rule.", "error"); return; }
      try {
        await post("/api/alerts/rules", {
          userId: userId, name: form.name.value, description: form.description.value,
          apps: apps, condition: form.condition.value, priority: form.priority.value,
          notificationMethod: form.notificationMethod.value, frequency: form.frequency.value, action: form.action.value
        });
        form.closest(".modal")?.classList.add("hidden");
        toast("Alert rule created.");
        loadSuggestions();
      } catch (error) { toast(error.message || "Unable to create alert rule.", "error"); }
    });

    document.addEventListener("click", async function (event) {
      const actionButton = event.target.closest("[data-alert-action]");
      if (actionButton) {
        try {
          await post("/api/alerts/actions", { userId: userId, alertId: actionButton.dataset.alertId, action: actionButton.dataset.alertAction });
          await loadAlerts();
        } catch (error) { toast(error.message, "error"); }
        return;
      }
      const aiButton = event.target.closest("[data-alert-ai]");
      if (aiButton) {
        const box = document.querySelector('[data-ai-result="' + aiButton.dataset.alertId + '"]');
        if (box) box.textContent = "Generating...";
        try {
          const data = await post("/api/alerts/ai", { alertId: aiButton.dataset.alertId, feature: aiButton.dataset.alertAi });
          if (box) box.textContent = data.result || "";
        } catch (error) { if (box) box.textContent = error.message; }
        return;
      }
      const suggestionButton = event.target.closest("[data-use-suggestion]");
      if (suggestionButton) {
        const suggestion = alertSuggestions[Number(suggestionButton.dataset.useSuggestion)];
        if (!suggestion) return;
        const form = document.querySelector("[data-alert-rule-form]");
        const modal = form?.closest(".modal");
        if (!form) return;
        modal?.classList.remove("hidden");
        form.name.value = suggestion.title;
        form.description.value = suggestion.description || "";
        form.condition.value = suggestion.condition || "";
        form.priority.value = suggestion.priority || "medium";
        form.action.value = suggestion.action || "notify";
        form.querySelectorAll('input[name="apps"]').forEach(function (box) { box.checked = (suggestion.apps || []).includes(box.value); });
      }
    });
  }

  function setupSettings() {
    const panel = document.querySelector("[data-settings-panel]");
    if (!panel) return;
    const form = panel.querySelector("[data-settings-form]");
    const status = panel.querySelector("[data-settings-status]");
    try {
      const stored = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "null");
      if (stored && form) Object.keys(stored).forEach(function (key) {
        const field = form.elements[key];
        if (!field) return;
        if (field.type === "checkbox") field.checked = Boolean(stored[key]);
        else field.value = stored[key];
      });
    } catch (error) { }

    function collect() {
      const result = {};
      Array.from(form.elements).forEach(function (field) {
        if (!field.name) return;
        if (field.type === "checkbox") result[field.name] = field.checked;
        else if (field.type !== "submit") result[field.name] = field.value;
      });
      result.alertMethods = ["in_app"];
      result.briefingChannels = ["in_app", "email"];
      return result;
    }

    async function save() {
      const values = collect();
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(values));
      if (status) status.textContent = "Saving...";
      try {
        await post("/api/assistant-settings", { userId: panel.dataset.userId, settings: values });
        if (status) status.textContent = "Saved";
      } catch (error) { if (status) status.textContent = error.message; }
    }

    panel.querySelector("[data-settings-save]")?.addEventListener("click", save);
    form?.addEventListener("submit", function (event) { event.preventDefault(); save(); });
    panel.querySelector("[data-settings-reset]")?.addEventListener("click", function () {
      localStorage.removeItem(SETTINGS_KEY);
      window.location.reload();
    });
    panel.querySelector("[data-settings-export]")?.addEventListener("click", function () {
      const blob = new Blob([JSON.stringify(collect(), null, 2)], { type: "application/json" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "omniscyn-settings.json";
      link.click();
    });
  }

  function setupPair() {
    const keyInput = document.querySelector("[data-pair-key]");
    const urlInput = document.querySelector("[data-pair-url]");
    const qr = document.querySelector("[data-pair-qr]");
    if (!keyInput) return;

    function payload() {
      return JSON.stringify({ app: "omniscyn", url: (urlInput?.value || "").replace(/\/$/, ""), key: keyInput.value });
    }

    function drawQr(text) {
      if (!qr) return;
      let hash = 0;
      for (let i = 0; i < text.length; i += 1) hash = ((hash << 5) - hash + text.charCodeAt(i)) | 0;
      let html = '<div class="qr-placeholder">';
      for (let row = 0; row < 11; row += 1) {
        html += '<div style="display:flex">';
        for (let col = 0; col < 11; col += 1) {
          hash = (hash * 1103515245 + 12345) & 0x7fffffff;
          html += '<span style="width:9px;height:9px;background:' + ((hash >> 7) % 2 ? "#c4b5fd" : "transparent") + '"></span>';
        }
        html += '</div>';
      }
      html += '</div>';
      qr.innerHTML = html;
    }

    keyInput.addEventListener("input", function () { drawQr(payload()); });
    urlInput?.addEventListener("input", function () { drawQr(payload()); });
    document.querySelector("[data-pair-generate]")?.addEventListener("click", function () {
      const bytes = new Uint8Array(32);
      crypto.getRandomValues(bytes);
      keyInput.value = Array.from(bytes).map(function (byte) { return byte.toString(16).padStart(2, "0"); }).join("");
      drawQr(payload());
    });
    document.querySelector("[data-pair-copy]")?.addEventListener("click", async function () {
      try {
        await navigator.clipboard.writeText(payload());
        const copied = document.querySelector("[data-pair-copied]");
        if (copied) copied.textContent = "Copied";
        toast("Pairing data copied.");
      } catch (error) { toast("Copy failed. Select and copy the pairing data manually.", "error"); }
    });
    drawQr(payload());
  }

  function setupBriefingDetail() {
    const root = document.querySelector("[data-briefing-detail]");
    if (!root) return;
    let briefing = {};
    let userId = "";
    try { briefing = JSON.parse(document.getElementById("briefing-data").textContent); } catch (error) { briefing = {}; }
    try { userId = JSON.parse(document.getElementById("briefing-user-id").textContent); } catch (error) { userId = ""; }
    const panel = document.querySelector("[data-compose-panel]");
    const form = document.querySelector("[data-compose-form]");
    const status = document.querySelector("[data-compose-status]");
    let composeType = "email";
    let composeItem = null;

    function openCompose(type, item) {
      composeType = type;
      composeItem = item;
      panel?.classList.remove("hidden");
      const title = document.querySelector("[data-compose-title]");
      const replyTo = document.querySelector("[data-compose-reply-to]");
      const to = form?.querySelector("[data-compose-to]");
      const subject = form?.querySelector("[data-compose-subject]");
      const subjectLabel = form?.querySelector(".compose-subject");
      const send = form?.querySelector("[data-compose-send]");
      if (title) title.textContent = type === "email" ? "Compose Email Reply" : "Compose Message";
      if (replyTo) replyTo.textContent = item.from || item.chatName || "";
      if (subjectLabel) subjectLabel.classList.toggle("hidden", type !== "email");
      if (to) to.value = type === "email" ? (item.from?.match(/<(.+)>/)?.[1] || item.from || "") : (item.from || "");
      if (subject) subject.value = "Re: " + (item.subject || "");
      if (send) send.textContent = type === "email" ? "Send Email" : "Send Message";
      if (form) form.body.value = "";
      panel?.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    document.querySelectorAll("[data-compose-item]").forEach(function (button) {
      button.addEventListener("click", function () {
        const category = button.dataset.composeItem === "email" ? "email" : "messages";
        const index = Number(button.dataset.itemIndex || 0);
        const item = (briefing.data && briefing.data[category] || [])[index];
        if (item) openCompose(button.dataset.composeItem === "email" ? "email" : "message", item);
      });
    });

    document.querySelector("[data-compose-close]")?.addEventListener("click", function () { panel?.classList.add("hidden"); });

    document.querySelector("[data-compose-ai]")?.addEventListener("click", async function () {
      if (status) status.textContent = "Drafting…";
      try {
        const data = await post("/api/briefing/ai-draft", {
          type: composeType === "email" ? "email" : "message",
          from: composeItem?.from || "",
          subject: composeItem?.subject || "",
          body: composeItem?.body || composeItem?.snippet || "",
          replyType: "reply"
        });
        if (form) form.body.value = data.draft || "";
        if (status) status.textContent = "";
      } catch (error) { if (status) status.textContent = "AI draft failed. Please try again."; }
    });

    form?.addEventListener("submit", async function (event) {
      event.preventDefault();
      const sendButton = form.querySelector("[data-compose-send]");
      if (sendButton) sendButton.textContent = "Sending…";
      try {
        if (composeType === "email") {
          const result = await post("/api/gmail-mcp", {
            method: "gmail_send_message", userId: userId,
            params: { to: form.to.value, subject: form.subject.value, body: form.body.value }
          });
          if (result.error) throw new Error(result.error.message);
        } else {
          const result = await post("/api/whatsapp-mcp", {
            method: "whatsapp_send_message", userId: userId,
            params: { to: form.to.value, body: form.body.value }
          });
          if (result.error) throw new Error(result.error.message);
        }
        if (status) status.textContent = composeType === "email" ? "Email Sent!" : "Message Sent!";
        form.body.value = "";
      } catch (error) {
        if (status) status.textContent = "Failed to send. Please try again.";
      } finally {
        if (sendButton) sendButton.textContent = composeType === "email" ? "Send Email" : "Send Message";
      }
    });
  }

  setupTheme();
  setupMenus();
  setupModals();
  setupWaitlist();
  setupSimulator();
  setupAuth();
  setupChat();
  setupOverview();
  setupBriefing();
  setupIntegrations();
  setupAlerts();
  setupSettings();
  setupPair();
  setupBriefingDetail();

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () { navigator.serviceWorker.register("/sw.js").catch(function () {}); });
  }
})();
