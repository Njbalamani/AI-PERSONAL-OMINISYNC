const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = Number(process.env.WHATSAPP_BRIDGE_PORT || 3001);
const SESSIONS_DIR = path.resolve(process.env.WHATSAPP_BRIDGE_SESSIONS || path.join(__dirname, "sessions"));
const PAIRING_TTL_MS = 3 * 60 * 1000;

const sessions = new Map();
const reconnecting = new Set();
let baileysModule = null;
let pinoFn = null;

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function withTimeout(promise, ms, fallback) {
  return Promise.race([
    Promise.resolve(promise).catch(() => fallback),
    delay(ms).then(() => fallback),
  ]);
}

async function loadBaileys() {
  if (!baileysModule) {
    const imported = await import("@whiskeysockets/baileys");
    baileysModule = imported.default || imported;
    if (!baileysModule.makeWASocket && baileysModule.default) baileysModule = baileysModule.default;
  }
  return baileysModule;
}

async function loadPino() {
  if (!pinoFn) {
    const imported = await import("pino");
    pinoFn = imported.default || imported.pino || imported;
  }
  return pinoFn;
}

function safeId(value) {
  return String(value || "default").replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 120);
}

function normalizePhone(value) {
  let digits = String(value || "").replace(/\D/g, "");
  if (digits.startsWith("00")) digits = digits.slice(2);
  digits = digits.replace(/^0+/, "");
  if (digits.length === 14 && digits.startsWith("9191")) digits = digits.slice(2);
  if (digits.length === 10 && /^[6-9]/.test(digits)) digits = "91" + digits;
  if (!/^[1-9]\d{7,14}$/.test(digits)) {
    throw new Error("Enter a valid phone number, with country code if outside India. Indian 10-digit numbers are accepted.");
  }
  return digits;
}

function toJid(value) {
  const raw = String(value || "").trim();
  if (raw.includes("@")) return raw;
  return normalizePhone(raw) + "@s.whatsapp.net";
}

function textOf(message) {
  return (
    message?.conversation ||
    message?.extendedTextMessage?.text ||
    message?.imageMessage?.caption ||
    message?.videoMessage?.caption ||
    message?.documentMessage?.title ||
    ""
  );
}

class SessionStore {
  constructor(sessionId, dir, phoneNumber) {
    this.sessionId = sessionId;
    this.file = path.join(dir, "store.json");
    this.phoneNumber = phoneNumber || "";
    this.chats = new Map();
    this.contacts = new Map();
    this.messages = new Map();
    this.persistTimer = null;
    this.load();
  }

  load() {
    try {
      const raw = JSON.parse(fs.readFileSync(this.file, "utf8"));
      this.phoneNumber = raw.phoneNumber || this.phoneNumber;
      this.chats = new Map(raw.chats || []);
      this.contacts = new Map(raw.contacts || []);
      this.messages = new Map(raw.messages || []);
    } catch (error) {
      this.chats = new Map();
      this.contacts = new Map();
      this.messages = new Map();
    }
  }

  persist() {
    if (this.persistTimer) clearTimeout(this.persistTimer);
    this.persistTimer = setTimeout(() => {
      try {
        fs.writeFileSync(this.file, JSON.stringify({
          phoneNumber: this.phoneNumber,
          chats: Array.from(this.chats.entries()),
          contacts: Array.from(this.contacts.entries()),
          messages: Array.from(this.messages.entries()),
        }));
      } catch (error) {
        void error;
      }
    }, 400);
  }

  chatName(jid) {
    const chat = this.chats.get(jid) || {};
    const contact = this.contacts.get(jid) || {};
    return chat.name || chat.subject || contact.name || contact.verifiedName || contact.notify || jid;
  }

  setChats(chats) {
    for (const chat of chats || []) {
      if (!chat?.id) continue;
      this.chats.set(chat.id, {
        id: chat.id,
        name: chat.name || chat.subject || "",
        subject: chat.subject || "",
        isGroup: Boolean(chat.id.endsWith("@g.us") || chat.isGroup),
      });
    }
    this.persist();
  }

  setContacts(contacts) {
    for (const contact of contacts || []) {
      if (!contact?.id) continue;
      this.contacts.set(contact.id, {
        id: contact.id,
        name: contact.name || contact.notify || "",
        verifiedName: contact.verifiedName || "",
        phone: contact.id.split("@")[0],
      });
    }
    this.persist();
  }

  addMessages(messages) {
    for (const message of messages || []) {
      const jid = message?.key?.remoteJid;
      const id = message?.key?.id;
      if (!jid || !id) continue;
      const contact = this.contacts.get(jid) || {};
      const item = {
        messageId: id,
        chatId: jid,
        chatName: this.chatName(jid),
        from: message.key.fromMe ? "You" : message.pushName || contact.name || jid.split("@")[0],
        body: textOf(message.message),
        timestamp: new Date(Number(message.messageTimestamp || Date.now() / 1000) * 1000).toISOString(),
        fromMe: Boolean(message.key.fromMe),
      };
      this.messages.set(id, item);
      const chatMessages = Array.from(this.messages.values()).filter((entry) => entry.chatId === jid);
      if (chatMessages.length > 100) {
        const stale = chatMessages.sort((a, b) => a.timestamp.localeCompare(b.timestamp)).slice(0, chatMessages.length - 100);
        for (const entry of stale) this.messages.delete(entry.messageId);
      }
      if (!this.chats.has(jid)) {
        this.chats.set(jid, { id: jid, name: item.chatName, subject: "", isGroup: jid.endsWith("@g.us") });
      }
    }
    this.persist();
  }

  allMessages() {
    return Array.from(this.messages.values()).sort((a, b) => a.timestamp.localeCompare(b.timestamp));
  }
}

function statusPayload(sessionId) {
  const session = sessions.get(sessionId);
  if (!session) {
    return { success: true, status: "disconnected", connected: false, isSimulated: false, phoneNumber: "", error: "" };
  }
  const now = Date.now();
  const code = session.pairingCode && session.pairingCodeExpiresAt > now ? session.pairingCode : "";
  return {
    success: true,
    status: session.status,
    connected: session.status === "connected",
    isSimulated: false,
    phoneNumber: session.phoneNumber || "",
    pairingCode: code,
    pairingCodeExpiresAt: code ? session.pairingCodeExpiresAt : 0,
    error: session.error || "",
  };
}

async function waitForPairingResult(session, ms) {
  const started = Date.now();
  while (Date.now() - started < ms) {
    if (session.status === "connected") return statusPayload(session.sessionId);
    if (session.pairingCode && session.pairingCodeExpiresAt > Date.now()) return statusPayload(session.sessionId);
    if (session.error && !session.pairingPromise) return statusPayload(session.sessionId);
    await delay(250);
  }
  return statusPayload(session.sessionId);
}

async function requestPairingWithRetry(session, phoneNumber) {
  if (session.auth.creds.registered) return;
  await withTimeout(
    typeof session.sock.waitForSocketOpen === "function" ? session.sock.waitForSocketOpen() : Promise.resolve(),
    5000,
    undefined,
  );
  let lastError = null;
  for (let attempt = 1; attempt <= 4; attempt += 1) {
    try {
      const code = await session.sock.requestPairingCode(phoneNumber);
      if (!code) throw new Error("WhatsApp did not return a pairing code.");
      session.pairingCode = String(code).toUpperCase();
      session.pairingCodeExpiresAt = Date.now() + PAIRING_TTL_MS;
      session.error = "";
      return;
    } catch (error) {
      lastError = error;
      await delay(1500 * attempt);
    }
  }
  session.status = "error";
  session.error = "Could not get a code from WhatsApp. Check the number and try again.";
  if (lastError) session.lastPairingError = String(lastError.message || lastError);
}

function ensurePairing(session, phoneNumber) {
  if (session.pairingPromise) return session.pairingPromise;
  if (!phoneNumber || session.auth.creds.registered) return Promise.resolve();
  session.pairingPromise = requestPairingWithRetry(session, phoneNumber).finally(() => {
    session.pairingPromise = null;
  });
  return session.pairingPromise;
}

async function stopSession(sessionId, removeFiles) {
  const session = sessions.get(sessionId);
  sessions.delete(sessionId);
  if (!session) {
    if (removeFiles) fs.rmSync(path.join(SESSIONS_DIR, safeId(sessionId)), { recursive: true, force: true });
    return;
  }
  try {
    await withTimeout(session.sock.logout(), 3000, undefined);
  } catch (error) {
    void error;
  }
  try {
    session.sock.end(undefined);
  } catch (error) {
    void error;
  }
  if (removeFiles) fs.rmSync(path.join(SESSIONS_DIR, safeId(sessionId)), { recursive: true, force: true });
}

async function createSession(sessionId, phoneNumber, fresh) {
  const { makeWASocket, useMultiFileAuthState, DisconnectReason } = await loadBaileys();
  const pino = await loadPino();
  const dir = path.join(SESSIONS_DIR, safeId(sessionId));
  if (fresh) fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });

  const { state, saveCreds } = await useMultiFileAuthState(dir);
  const sock = makeWASocket({
    auth: state,
    logger: pino({ level: "silent" }),
    browser: ["Ubuntu", "Chrome", "22.04.4"],
    printQRInTerminal: false,
  });
  const store = new SessionStore(sessionId, dir, phoneNumber);
  const session = {
    sessionId,
    sock,
    auth: state,
    store,
    status: "connecting",
    phoneNumber: phoneNumber || "",
    pairingCode: "",
    pairingCodeExpiresAt: 0,
    pairingPromise: null,
    error: "",
    everConnected: false,
  };
  sessions.set(sessionId, session);

  sock.ev.on("creds.update", saveCreds);
  sock.ev.on("messaging-history.set", (history) => {
    store.setChats(history.chats);
    store.setContacts(history.contacts);
    store.addMessages(history.messages);
  });
  sock.ev.on("chats.upsert", (chats) => store.setChats(chats));
  sock.ev.on("chats.update", (chats) => store.setChats(chats));
  sock.ev.on("contacts.upsert", (contacts) => store.setContacts(contacts));
  sock.ev.on("contacts.update", (contacts) => store.setContacts(contacts));
  sock.ev.on("messages.upsert", (event) => {
    if (event.type === "notify" || event.type === "append") store.addMessages(event.messages);
  });
  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect } = update;
    if (connection === "connecting") {
      session.status = "connecting";
      void ensurePairing(session, session.phoneNumber);
      return;
    }
    if (connection === "open") {
      session.status = "connected";
      session.everConnected = true;
      session.pairingCode = "";
      session.pairingCodeExpiresAt = 0;
      session.error = "";
      return;
    }
    if (connection === "close") {
      session.status = "disconnected";
      session.pairingCode = "";
      session.pairingCodeExpiresAt = 0;
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      if (statusCode === DisconnectReason.loggedOut) {
        fs.rmSync(dir, { recursive: true, force: true });
        session.error = "WhatsApp session was logged out. Connect again to get a new code.";
        return;
      }
      if (session.everConnected && !reconnecting.has(sessionId)) {
        reconnecting.add(sessionId);
        setTimeout(async () => {
          try {
            await createSession(sessionId, session.phoneNumber, false);
          } catch (error) {
            void error;
          } finally {
            reconnecting.delete(sessionId);
          }
        }, 2000);
      }
    }
  });

  if (!state.creds.registered && phoneNumber) await ensurePairing(session, phoneNumber);
  return session;
}

function recentMessages(session, limit) {
  const byChat = new Map();
  for (const message of session.store.allMessages()) {
    byChat.set(message.chatId, message);
  }
  return Array.from(byChat.values()).sort((a, b) => b.timestamp.localeCompare(a.timestamp)).slice(0, limit);
}

async function dispatchMcp(sessionId, method, params) {
  const session = sessions.get(sessionId);
  if (!session) throw { code: -32002, message: "WhatsApp integration is not connected." };
  if (session.status !== "connected") throw { code: -32002, message: `WhatsApp connection is currently ${session.status}. Please check details.` };
  const store = session.store;

  if (method === "whatsapp_get_recent_messages") {
    return { messages: recentMessages(session, Number(params.limit || 20)) };
  }
  if (method === "whatsapp_get_chat_history") {
    if (!params.chatId) throw { code: -32602, message: "Argument 'chatId' is required" };
    const rows = store.allMessages().filter((item) => item.chatId === params.chatId).slice(-Number(params.limit || 20));
    return { messages: rows };
  }
  if (method === "whatsapp_send_message") {
    if (!params.to || !params.body) throw { code: -32602, message: "Arguments 'to' and 'body' are required" };
    const jid = toJid(params.to);
    const result = await session.sock.sendMessage(jid, { text: String(params.body) });
    const item = {
      messageId: result?.key?.id || `wa_${Date.now()}`,
      chatId: jid,
      chatName: store.chatName(jid),
      from: "You",
      body: String(params.body),
      timestamp: new Date().toISOString(),
      fromMe: true,
    };
    store.addMessages([{ key: { remoteJid: jid, id: item.messageId, fromMe: true }, message: { conversation: item.body }, messageTimestamp: Date.now() / 1000, pushName: "You" }]);
    return { success: true, message: "WhatsApp message sent successfully", messageId: item.messageId, recipient: jid };
  }
  if (method === "whatsapp_search_chats") {
    const query = String(params.query || "").toLowerCase();
    const chats = Array.from(store.chats.values()).filter((chat) => chat.name.toLowerCase().includes(query) || chat.id.toLowerCase().includes(query));
    return { chats: chats.map((chat) => ({ chatId: chat.id, name: chat.name || chat.id, isGroup: chat.isGroup })) };
  }
  if (method === "whatsapp_summarize_conversations") {
    const rows = params.chatId ? store.allMessages().filter((item) => item.chatId === params.chatId) : recentMessages(session, 20);
    const summary = rows.length
      ? "OmniSync WhatsApp Brief\n" + rows.map((item) => `• ${item.chatName}: ${item.body}`).join("\n")
      : "No WhatsApp messages are available yet.";
    return { summary };
  }
  if (method === "whatsapp_get_contact_details") {
    if (!params.jidOrPhone) throw { code: -32602, message: "Argument 'jidOrPhone' is required" };
    const needle = String(params.jidOrPhone);
    const jid = needle.includes("@") ? needle : toJid(needle);
    const contact = store.contacts.get(jid) || {};
    return { id: jid, name: contact.name || store.chatName(jid), phone: contact.phone || jid.split("@")[0], verifiedName: contact.verifiedName || "" };
  }
  if (method === "whatsapp_list_groups") {
    return { groups: Array.from(store.chats.values()).filter((chat) => chat.isGroup).map((chat) => ({ id: chat.id, name: chat.name || chat.id })) };
  }
  if (method === "whatsapp_get_group_messages") {
    if (!params.groupId) throw { code: -32602, message: "Argument 'groupId' is required" };
    const rows = store.allMessages().filter((item) => item.chatId === params.groupId).slice(-Number(params.limit || 20));
    return { messages: rows };
  }
  if (method === "whatsapp_send_group_messages" || method === "whatsapp_send_group_message") {
    if (!params.groupId || !params.body) throw { code: -32602, message: "Arguments 'groupId' and 'body' are required" };
    const jid = toJid(params.groupId);
    const result = await session.sock.sendMessage(jid, { text: String(params.body) });
    return { success: true, message: "WhatsApp group message sent successfully", messageId: result?.key?.id || `wa_${Date.now()}`, recipientGroup: jid };
  }
  throw { code: -32601, message: `Method not found: ${method}` };
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    let raw = "";
    request.on("data", (chunk) => {
      raw += chunk;
      if (raw.length > 1024 * 1024) reject(new Error("Request too large."));
    });
    request.on("end", () => {
      try {
        resolve(raw ? JSON.parse(raw) : {});
      } catch (error) {
        reject(error);
      }
    });
    request.on("error", reject);
  });
}

function sendJson(response, statusCode, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(statusCode, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) });
  response.end(body);
}

const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, `http://127.0.0.1:${PORT}`);
    if (request.method === "GET" && url.pathname === "/health") {
      return sendJson(response, 200, { ok: true, sessions: sessions.size });
    }
    if (request.method !== "POST") {
      return sendJson(response, 404, { error: { code: -32600, message: "Not found" } });
    }
    const body = await readBody(request);
    const sessionId = safeId(body.sessionId);

    if (url.pathname === "/connect") {
      const phoneNumber = normalizePhone(body.phoneNumber);
      await stopSession(sessionId, true);
      const session = await createSession(sessionId, phoneNumber, true);
      const result = await withTimeout(waitForPairingResult(session, 20000), 21000, statusPayload(sessionId));
      return sendJson(response, 200, result);
    }
    if (url.pathname === "/status") {
      if (!sessions.has(sessionId)) {
        const dir = path.join(SESSIONS_DIR, sessionId);
        try {
          const creds = JSON.parse(fs.readFileSync(path.join(dir, "creds.json"), "utf8"));
          const stored = JSON.parse(fs.readFileSync(path.join(dir, "store.json"), "utf8"));
          if (creds.registered) {
            const phone = stored.phoneNumber || "";
            await createSession(sessionId, phone, false);
          }
        } catch (error) {
          void error;
        }
      }
      return sendJson(response, 200, statusPayload(sessionId));
    }
    if (url.pathname === "/disconnect") {
      await stopSession(sessionId, true);
      return sendJson(response, 200, { success: true, status: "disconnected" });
    }
    if (url.pathname === "/mcp") {
      try {
        const result = await dispatchMcp(sessionId, body.method, body.params || {});
        return sendJson(response, 200, { result });
      } catch (error) {
        const payload = error && typeof error === "object" && error.code
          ? error
          : { code: -32603, message: String(error?.message || error || "Internal Server Error") };
        return sendJson(response, 400, { error: payload });
      }
    }
    return sendJson(response, 404, { error: { code: -32600, message: "Not found" } });
  } catch (error) {
    return sendJson(response, 500, { error: { code: -32603, message: String(error?.message || error || "Internal Server Error") } });
  }
});

fs.mkdirSync(SESSIONS_DIR, { recursive: true });
server.listen(PORT, "127.0.0.1", () => {
  process.stdout.write(`WhatsApp bridge listening on http://127.0.0.1:${PORT}\n`);
});
