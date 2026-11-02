(() => {
    const toggle = document.getElementById("chat-toggle");
    const panel = document.getElementById("business-chat");
    const closeButton = document.getElementById("chat-close");
    const messagesElement = document.getElementById("chat-messages");
    const form = document.getElementById("chat-form");
    const input = document.getElementById("chat-input");
    const sendButton = document.getElementById("chat-send");

    if (!toggle || !panel || !closeButton || !messagesElement || !form || !input || !sendButton) {
        return;
    }

    const conversation = [];
    let historyLoaded = false;
    let historyPromise = null;
    let isSending = false;

    function appendMessage(role, content) {
        const element = document.createElement("div");
        element.className = `chat-message chat-message-${role}`;
        element.textContent = String(content);
        messagesElement.appendChild(element);
        messagesElement.scrollTop = messagesElement.scrollHeight;
    }

    function addConversationMessage(role, content) {
        conversation.push({ role, content: String(content).slice(0, 2000) });
        if (conversation.length > 12) {
            conversation.splice(0, conversation.length - 12);
        }
    }

    async function loadHistory() {
        if (historyLoaded) return;
        if (historyPromise) return historyPromise;

        historyPromise = (async () => {
            try {
                const response = await fetch(panel.dataset.historyUrl, {
                    credentials: "same-origin",
                    cache: "no-store",
                    headers: { Accept: "application/json" }
                });
                const data = await response.json();
                if (!response.ok || !data.success || !Array.isArray(data.messages)) {
                    throw new Error(data.error || "No se pudo cargar la conversación.");
                }

                const messages = data.messages.filter(
                    (message) =>
                        message &&
                        (message.role === "user" || message.role === "assistant") &&
                        typeof message.content === "string"
                );
                messagesElement.replaceChildren();
                for (const message of messages.slice(-12)) {
                    addConversationMessage(message.role, message.content);
                    appendMessage(message.role, message.content);
                }
                if (messages.length === 0) {
                    appendMessage("assistant", "¡Hola! ¿En qué puedo ayudarte?");
                }
                historyLoaded = true;
            } catch {
                appendMessage("error", "No pudimos cargar el historial. Podés enviar una consulta nueva.");
            } finally {
                historyPromise = null;
            }
        })();
        return historyPromise;
    }

    function openChat() {
        panel.hidden = false;
        toggle.setAttribute("aria-expanded", "true");
        input.focus();
        if (!historyLoaded) {
            void loadHistory();
        }
    }

    function closeChat() {
        panel.hidden = true;
        toggle.setAttribute("aria-expanded", "false");
        toggle.focus();
    }

    toggle.addEventListener("click", openChat);
    closeButton.addEventListener("click", closeChat);
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !panel.hidden) {
            closeChat();
        }
    });

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const message = input.value.trim();
        if (!message || isSending) {
            return;
        }

        if (!historyLoaded) {
            await loadHistory();
        }

        appendMessage("user", message);
        addConversationMessage("user", message);
        input.value = "";
        isSending = true;
        input.disabled = true;
        sendButton.disabled = true;

        try {
            const response = await fetch(panel.dataset.chatUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    Accept: "application/json",
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    message,
                    conversation: conversation.slice(-12)
                })
            });
            const data = await response.json();
            if (!response.ok || !data.success || typeof data.response !== "string") {
                throw new Error(data.error || "No pudimos procesar tu consulta.");
            }

            appendMessage("assistant", data.response);
            addConversationMessage("assistant", data.response);
        } catch (error) {
            appendMessage(
                "error",
                error instanceof Error
                    ? error.message
                    : "No pudimos conectar con el asistente. Intentá nuevamente."
            );
        } finally {
            isSending = false;
            input.disabled = false;
            sendButton.disabled = false;
            input.focus();
        }
    });
})();
