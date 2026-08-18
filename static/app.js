// ============================================================
// EL CORTE - TURNOBOT
// ============================================================
//
// Arquitectura:
//
// SIN IA:
// - Servicios
// - Disponibilidad
// - Reservar desde formulario
// - Mis turnos
// - Cancelar
// - Reprogramar
//
// CON IA:
// - Texto libre
// - Consultas ambiguas
// - Conversación natural
//
// ============================================================


// ============================================================
// ELEMENTOS DEL DOM
// ============================================================

const chat = document.getElementById("chat");
const messageInput = document.getElementById("message");
const sendButton = document.getElementById("send");


// ============================================================
// CONFIGURACIÓN
// ============================================================

const API = {
    servicios: "/api/servicios",
    disponibilidad: "/api/disponibilidad",
    turnos: "/api/turnos",
    reservar: "/api/reservar",
    cancelar: "/api/cancelar",
    reprogramar: "/api/reprogramar",
    chat: "/chat"
};


// ============================================================
// CONFIGURACIÓN DE CONVERSACIÓN
// ============================================================

const MAX_HISTORY_MESSAGES = 12;

let conversation = [];


// ============================================================
// MENSAJES
// ============================================================

function addMessage(text, type = "bot") {

    const message = document.createElement("div");

    message.className = `message ${type}`;


    const bubble = document.createElement("div");

    bubble.className = "bubble";

    bubble.textContent = String(text ?? "");


    message.appendChild(bubble);

    chat.appendChild(message);


    scrollChat();
}


// ============================================================
// SCROLL
// ============================================================

function scrollChat() {

    chat.scrollTop = chat.scrollHeight;
}


// ============================================================
// INDICADOR DE CARGA
// ============================================================

function showTyping(text = "Consultando...") {

    hideTyping();


    const message = document.createElement("div");

    message.id = "typing-message";

    message.className =
        "message bot typing-message";


    const bubble = document.createElement("div");

    bubble.className = "bubble";

    bubble.textContent = text;


    message.appendChild(bubble);

    chat.appendChild(message);


    scrollChat();
}


function hideTyping() {

    const typing =
        document.getElementById(
            "typing-message"
        );


    if (typing) {

        typing.remove();

    }
}


// ============================================================
// HELPER: OPERACIONES CON INDICADOR
// ============================================================

async function withTyping(
    loadingText,
    task
) {

    showTyping(loadingText);


    try {

        return await task();

    } catch (error) {

        console.error(error);


        addMessage(
            error.message ||
            "No pude conectarme con el servidor.",
            "bot"
        );


        return null;

    } finally {

        hideTyping();

    }
}


// ============================================================
// PETICIONES JSON
// ============================================================

async function fetchJSON(
    url,
    options = {}
) {

    const response =
        await fetch(
            url,
            options
        );


    if (!response.ok) {

        let serverMessage = "";


        try {

            const errorData =
                await response.json();


            serverMessage =
                errorData.error ||
                errorData.message ||
                "";

        } catch {

            // El servidor pudo devolver HTML,
            // texto plano o una respuesta vacía.

        }


        throw new Error(
            serverMessage ||
            `Error HTTP ${response.status}`
        );
    }


    try {

        return await response.json();

    } catch {

        throw new Error(
            "El servidor devolvió una respuesta no válida."
        );
    }
}


// ============================================================
// DESACTIVAR BOTONES
// ============================================================

function disableButtons(container) {

    if (!container) {

        return;

    }


    container
        .querySelectorAll("button")
        .forEach(button => {

            button.disabled = true;

        });
}


// ============================================================
// FECHA ACTUAL ARGENTINA
// ============================================================

function getArgentinaDate() {

    const formatter =
        new Intl.DateTimeFormat(
            "en-CA",
            {
                timeZone:
                    "America/Argentina/Buenos_Aires",

                year: "numeric",

                month: "2-digit",

                day: "2-digit"
            }
        );


    return formatter.format(
        new Date()
    );
}


// ============================================================
// SUMAR DÍAS
// ============================================================

function addDays(
    dateString,
    days
) {

    const [year, month, day] =
        dateString
            .split("-")
            .map(Number);


    const date =
        new Date(
            Date.UTC(
                year,
                month - 1,
                day,
                12
            )
        );


    date.setUTCDate(
        date.getUTCDate() + days
    );


    return [

        date.getUTCFullYear(),

        String(
            date.getUTCMonth() + 1
        ).padStart(2, "0"),

        String(
            date.getUTCDate()
        ).padStart(2, "0")

    ].join("-");
}


// ============================================================
// FORMATEAR FECHA
// ============================================================

function formatDate(
    dateString
) {

    if (
        typeof dateString !== "string"
    ) {

        return "";

    }


    const parts =
        dateString.split("-");


    if (
        parts.length !== 3
    ) {

        return dateString;

    }


    return (
        `${parts[2]}/${parts[1]}/${parts[0]}`
    );
}


// ============================================================
// NOMBRE DEL DÍA
// ============================================================

function getDayName(
    dateString
) {

    if (
        typeof dateString !== "string"
    ) {

        return "";

    }


    const [
        year,
        month,
        day
    ] =
        dateString
            .split("-")
            .map(Number);


    if (
        !year ||
        !month ||
        !day
    ) {

        return "";

    }


    const date =
        new Date(
            Date.UTC(
                year,
                month - 1,
                day,
                12
            )
        );


    return new Intl.DateTimeFormat(
        "es-AR",
        {
            weekday: "long",

            timeZone:
                "America/Argentina/Buenos_Aires"
        }
    ).format(date);
}


// ============================================================
// DETECTOR LOCAL DE INTENCIÓN
// ============================================================

function normalizeText(text) {

    return text
        .toLowerCase()
        .normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "")
        .trim();
}


// ============================================================
// DETECTAR FECHA
// ============================================================

function extractDate(text) {

    const normalized =
        normalizeText(text);


    const today =
        getArgentinaDate();


    if (
        /\bhoy\b/.test(normalized)
    ) {

        return today;

    }


    // IMPORTANTE:
    // Primero comprobamos "pasado mañana"
    // antes que "mañana".

    if (
        /\bpasado manana\b/.test(normalized)
    ) {

        return addDays(
            today,
            2
        );

    }


    if (
        /\bmanana\b/.test(normalized)
    ) {

        return addDays(
            today,
            1
        );

    }


    // YYYY-MM-DD

    const isoMatch =
        normalized.match(
            /\b(20\d{2})-(\d{2})-(\d{2})\b/
        );


    if (isoMatch) {

        return (
            `${isoMatch[1]}-${isoMatch[2]}-${isoMatch[3]}`
        );

    }


    // DD/MM/YYYY o DD-MM-YYYY

    const dateMatch =
        normalized.match(
            /\b(\d{1,2})[\/\-](\d{1,2})[\/\-](20\d{2})\b/
        );


    if (dateMatch) {

        const day =
            dateMatch[1].padStart(2, "0");


        const month =
            dateMatch[2].padStart(2, "0");


        const year =
            dateMatch[3];


        return (
            `${year}-${month}-${day}`
        );

    }


    // DÍAS DE LA SEMANA

    const weekdays = {

        domingo: 0,

        lunes: 1,

        martes: 2,

        miercoles: 3,

        jueves: 4,

        viernes: 5,

        sabado: 6

    };


    for (
        const [dayName, dayIndex]
        of Object.entries(weekdays)
    ) {

        const regex =
            new RegExp(
                `\\b${dayName}\\b`
            );


        if (
            regex.test(normalized)
        ) {

            return nextWeekday(
                today,
                dayIndex
            );

        }

    }


    return null;
}


// ============================================================
// CALCULAR PRÓXIMO DÍA DE LA SEMANA
// ============================================================

function nextWeekday(
    currentDateString,
    targetDay
) {

    const [
        year,
        month,
        day
    ] =
        currentDateString
            .split("-")
            .map(Number);


    const currentDate =
        new Date(
            Date.UTC(
                year,
                month - 1,
                day,
                12
            )
        );


    const currentDay =
        currentDate.getUTCDay();


    let difference =
        targetDay -
        currentDay;


    if (
        difference < 0
    ) {

        difference += 7;

    }


    return addDays(
        currentDateString,
        difference
    );
}


// ============================================================
// DETECTAR SERVICIO
// ============================================================

function extractService(text) {

    const normalized =
        normalizeText(text);


    // CORTE + BARBA
    if (
        normalized.includes("corte + barba") ||
        normalized.includes("corte y barba") ||
        normalized.includes("corte barba")
    ) {

        return "Corte + barba";
    }


    // BARBA
    if (
        /\bbarba\b/.test(normalized) &&
        !normalized.includes("corte")
    ) {

        return "Barba";
    }


    // CORTE
    if (
        /\bcorte\b/.test(normalized) ||
        normalized.includes("cortarme el pelo") ||
        normalized.includes("cortarme el cabello") ||
        normalized.includes("cortar el pelo") ||
        normalized.includes("cortar el cabello") ||
        normalized.includes("quiero cortarme")
    ) {

        return "Corte";
    }


    return null;
}


// ============================================================
// DETECTAR HORA
// ============================================================

function extractTime(text) {

    const normalized =
        normalizeText(text);

    const isPM =
        /\b(tarde|noche)\b/.test(normalized);

    function adjustHour(hour) {

        if (
            isPM &&
            hour < 12
        ) {
            return hour + 12;
        }

        return hour;
    }
    // "después de las 5" / "a partir de las 5"
// En este negocio, horas chicas en este contexto
// se interpretan como horas de la tarde.
let afterMatch =
    normalized.match(
        /(?:despues|después|a partir)\s+de\s+las?\s+([01]?\d|2[0-3])\b/
    );

if (afterMatch) {

    let hour =
        parseInt(
            afterMatch[1],
            10
        );

    if (hour >= 1 && hour <= 7) {
        hour += 12;
    }

    return (
        `${String(hour).padStart(2, "0")}:00`
    );
}

    let match =
        normalized.match(
            /\b([01]?\d|2[0-3]):([0-5]\d)\b/
        );

    if (match) {

        const hour =
            adjustHour(
                parseInt(match[1], 10)
            );

        return (
            `${String(hour).padStart(2, "0")}:${match[2]}`
        );
    }

    match =
        normalized.match(
            /(?:a\s+las|las)\s+([01]?\d|2[0-3])\b/
        );

    if (match) {

        const hour =
            adjustHour(
                parseInt(match[1], 10)
            );

        return (
            `${String(hour).padStart(2, "0")}:00`
        );
    }

    match =
        normalized.match(
            /\b([01]?\d|2[0-3])\s*(?:hs|horas)\b/
        );

    if (match) {

        const hour =
            adjustHour(
                parseInt(match[1], 10)
            );

        return (
            `${String(hour).padStart(2, "0")}:00`
        );
    }

    return null;
}

// ============================================================
// DETECTAR INTENCIÓN
// ============================================================

function detectIntent(text) {

    const normalized =
        normalizeText(text);


    // CANCELAR

    if (
        /\b(cancelar|cancele|cancela|anular|anula|anulado)\b/
            .test(normalized)
    ) {

        return "cancel";

    }


    // REPROGRAMAR

    if (
        /\b(reprogramar|reprograma|cambiar|cambio|mover|muevas)\b/
            .test(normalized)
    ) {

        return "reschedule";

    }


    // MIS TURNOS

    if (
        normalized.includes("mis turnos") ||
        normalized.includes("mi turno") ||
        normalized.includes("turnos a mi nombre") ||
        normalized.includes("que turnos tengo")
    ) {

        return "appointments";

    }


    // SERVICIOS

    if (
        /\b(servicio|servicios|precio|precios|cuesta|cuestan|tarifa)\b/
            .test(normalized)
    ) {

        return "services";

    }


    // RESERVAR

   if (
    /\b(reservar|reserva|reservame|sacar turno|quiero un turno|necesito un turno)\b/
        .test(normalized) ||
    normalized.includes("quiero cortarme el pelo") ||
    normalized.includes("quiero cortarme el cabello") ||
    normalized.includes("necesito cortarme el pelo") ||
    normalized.includes("necesito cortarme el cabello") ||
    normalized.includes("quiero cortar el pelo") ||
    normalized.includes("quiero cortar el cabello")
) {
    return "reservation";
}

    // DISPONIBILIDAD

    if (
        /\b(disponibilidad|horario|horarios|disponible|disponibles)\b/
            .test(normalized)
    ) {

        return "availability";

    }


    // PREGUNTAS DE TURNO/LUGAR

    if (
        normalized.includes("hay turno") ||
        normalized.includes("hay algun turno") ||
        normalized.includes("queda algun turno") ||
        normalized.includes("hay lugar")
    ) {

        return "availability";

    }


    // SOLO FECHA

    const date =
        extractDate(text);


    if (date) {

        return "date_only";

    }


    return "unknown";
}


// ============================================================
// OPCIONES CUANDO SOLO SE MENCIONÓ UNA FECHA
// ============================================================

function showDateIntentOptions(
    date
) {

    const message =
        document.createElement("div");


    message.className =
        "message bot";


    const bubble =
        document.createElement("div");


    bubble.className =
        "bubble availability-options";


    const title =
        document.createElement("div");


    title.className =
        "availability-title";


    title.textContent =
        `Entendí que hablás del ${getDayName(date)} ${formatDate(date)}.`;


    const question =
        document.createElement("p");


    question.className =
        "appointment-subtitle";


    question.textContent =
        "¿Qué querés hacer?";


    const buttons =
        document.createElement("div");


    buttons.className =
        "availability-buttons";


    const availabilityButton =
        document.createElement("button");


    availabilityButton.textContent =
        "Ver horarios";


    availabilityButton.addEventListener(
        "click",
        async () => {

            disableButtons(
                buttons
            );


            addMessage(
                `Quiero consultar los horarios del ${formatDate(date)}.`,
                "user"
            );


            await checkAvailability(
                date
            );

        }
    );


    const reservationButton =
        document.createElement("button");


    reservationButton.textContent =
        "Reservar";


    reservationButton.addEventListener(
        "click",
        async () => {

            disableButtons(
                buttons
            );


            addMessage(
                `Quiero reservar un turno para el ${formatDate(date)}.`,
                "user"
            );


            await showReservationForm(
                date
            );

        }
    );


    buttons.appendChild(
        availabilityButton
    );


    buttons.appendChild(
        reservationButton
    );


    bubble.appendChild(title);

    bubble.appendChild(question);

    bubble.appendChild(buttons);


    message.appendChild(bubble);

    chat.appendChild(message);


    scrollChat();
}


// ============================================================
// PROCESAR INTENCIÓN LOCAL
// ============================================================
// ============================================================
// DETECTAR MÚLTIPLES INTENCIONES
// ============================================================

function hasMultipleIntentions(text) {

    const normalized =
        normalizeText(text);

    const intents = [];

    if (
        /\b(reservar|reserva|reservame|sacar turno|quiero un turno|necesito un turno)\b/
            .test(normalized)
    ) {

        intents.push("reservation");
    }

    if (
        /\b(servicio|servicios|precio|precios|cuesta|cuestan|tarifa)\b/
            .test(normalized)
    ) {

        intents.push("services");
    }

    if (
        /\b(disponibilidad|horario|horarios|disponible|disponibles)\b/
            .test(normalized)
    ) {

        intents.push("availability");
    }

    return intents.length > 1;
}
async function handleLocalIntent(
    text
) {
        if (
        hasMultipleIntentions(text)
    ) {

        return false;
    }

    const intent =
        detectIntent(text);


    const date =
        extractDate(text);


    const service =
        extractService(text);


    const time =
        extractTime(text);


    


    // SERVICIOS

    if (
        intent === "services"
    ) {

        await showServices();

        return true;

    }


    // DISPONIBILIDAD

    if (
        intent === "availability"
    ) {

        if (date) {

            await checkAvailability(
                date
            );

        } else {

            showAvailabilityOptions();

        }


        return true;
    }


    // RESERVAR

    if (
        intent === "reservation"
    ) {

        await showReservationForm(
            date || "",
            time || "",
            service || ""
        );


        return true;
    }


    // MIS TURNOS

    if (
        intent === "appointments"
    ) {

        showMyAppointments();

        return true;
    }


    // CANCELAR

    if (
        intent === "cancel"
    ) {

        addMessage(
            "Claro. Primero busquemos tus turnos para que puedas elegir cuál cancelar.",
            "bot"
        );


        showMyAppointments();

        return true;
    }


    // REPROGRAMAR

    if (
        intent === "reschedule"
    ) {

        addMessage(
            "Claro. Primero busquemos tus turnos para que puedas elegir cuál reprogramar.",
            "bot"
        );


        showMyAppointments();

        return true;
    }


    // SOLO FECHA

    if (
        intent === "date_only" &&
        date
    ) {

        showDateIntentOptions(
            date
        );


        return true;
    }


    return false;
}
// ============================================================
// SERVICIOS
// ============================================================
// NO USA GEMINI
// ============================================================

async function showServices() {

    const data =
        await withTyping(
            "Consultando servicios...",
            () =>
                fetchJSON(
                    API.servicios
                )
        );


    if (!data) {

        return;

    }


    if (!data.success) {

        addMessage(
            data.error ||
            "No pude consultar los servicios.",
            "bot"
        );

        return;
    }


    const message =
        document.createElement("div");


    message.className =
        "message bot";


    const container =
        document.createElement("div");


    container.className =
        "service-list";


    const title =
        document.createElement("div");


    title.className =
        "service-list-title";


    title.textContent =
        "Nuestros servicios";


    container.appendChild(title);


    data.servicios.forEach(
        service => {

            const card =
                document.createElement("div");


            card.className =
                "service-card";


            const name =
                document.createElement("span");


            name.className =
                "service-name";


            name.textContent =
                `✂ ${service.nombre}`;


            const price =
                document.createElement("span");


            price.className =
                "service-price";


            price.textContent =
                `$${Number(
                    service.precio
                ).toLocaleString("es-AR")}`;


            card.appendChild(name);

            card.appendChild(price);


            container.appendChild(card);

        }
    );


    message.appendChild(
        container
    );


    chat.appendChild(
        message
    );


    scrollChat();
}


// ============================================================
// DISPONIBILIDAD
// ============================================================
// NO USA GEMINI
// ============================================================

async function checkAvailability(
    date
) {

    const data =
        await withTyping(
            "Consultando horarios...",
            () =>
                fetchJSON(
                    `${API.disponibilidad}/${date}`
                )
        );


    if (!data) {

        return;

    }


    if (!data.success) {

        addMessage(
            data.error ||
            "No pude consultar la disponibilidad.",
            "bot"
        );

        return;
    }


    const times =
        data.horarios_disponibles || [];


    if (
        times.length === 0
    ) {

        addMessage(
            `No quedan horarios disponibles para el ${getDayName(date)} ${formatDate(date)}.`,
            "bot"
        );

        return;
    }


    const message =
        document.createElement("div");


    message.className =
        "message bot";


    const container =
        document.createElement("div");


    container.className =
        "availability-result";


    const title =
        document.createElement("div");


    title.className =
        "availability-title";


    title.textContent =
        `Disponibilidad · ${getDayName(date)} ${formatDate(date)}`;


    const timeGrid =
        document.createElement("div");


    timeGrid.className =
        "time-grid";


    times.forEach(
        time => {

            const button =
                document.createElement("button");


            button.className =
                "time-button";


            button.textContent =
                `${time} hs`;


            button.addEventListener(
                "click",
                async () => {

                    disableButtons(
                        timeGrid
                    );


                    addMessage(
                        `Quiero reservar para el ${formatDate(date)} a las ${time} hs.`,
                        "user"
                    );


                    await showReservationForm(
                        date,
                        time
                    );

                }
            );


            timeGrid.appendChild(button);

        }
    );


    container.appendChild(
        title
    );


    container.appendChild(
        timeGrid
    );


    message.appendChild(
        container
    );


    chat.appendChild(
        message
    );


    scrollChat();
}


// ============================================================
// OPCIONES DE DISPONIBILIDAD
// ============================================================

function showAvailabilityOptions() {

    const message =
        document.createElement("div");


    message.className =
        "message bot";


    const bubble =
        document.createElement("div");


    bubble.className =
        "bubble availability-options";


    const title =
        document.createElement("div");


    title.className =
        "availability-title";


    title.textContent =
        "¿Para qué día querés consultar?";


    const buttons =
        document.createElement("div");


    buttons.className =
        "availability-buttons";


    const today =
        document.createElement("button");


    today.textContent =
        "Hoy";


    today.addEventListener(
        "click",
        async () => {

            disableButtons(
                buttons
            );


            addMessage(
                "Quiero consultar la disponibilidad de hoy.",
                "user"
            );


            await checkAvailability(
                getArgentinaDate()
            );

        }
    );


    const tomorrow =
        document.createElement("button");


    tomorrow.textContent =
        "Mañana";


    tomorrow.addEventListener(
        "click",
        async () => {

            disableButtons(
                buttons
            );


            addMessage(
                "Quiero consultar la disponibilidad de mañana.",
                "user"
            );


            await checkAvailability(
                addDays(
                    getArgentinaDate(),
                    1
                )
            );

        }
    );


    const chooseDate =
        document.createElement("button");


    chooseDate.textContent =
        "Elegir fecha";


    chooseDate.addEventListener(
        "click",
        () => {

            disableButtons(
                buttons
            );


            showDatePicker();

        }
    );


    buttons.appendChild(today);

    buttons.appendChild(tomorrow);

    buttons.appendChild(chooseDate);


    bubble.appendChild(title);

    bubble.appendChild(buttons);


    message.appendChild(bubble);


    chat.appendChild(message);


    scrollChat();
}


// ============================================================
// SELECTOR DE FECHA
// ============================================================

function showDatePicker() {

    const message =
        document.createElement("div");


    message.className =
        "message bot";


    const bubble =
        document.createElement("div");


    bubble.className =
        "bubble date-picker";


    const title =
        document.createElement("div");


    title.className =
        "availability-title";


    title.textContent =
        "Elegí una fecha";


    const input =
        document.createElement("input");


    input.type =
        "date";


    input.min =
        getArgentinaDate();


    const button =
        document.createElement("button");


    button.textContent =
        "Consultar disponibilidad";


    button.addEventListener(
        "click",
        async () => {

            if (!input.value) {

                input.focus();

                return;
            }


            const date =
                input.value;


            addMessage(
                `Quiero consultar la disponibilidad del ${formatDate(date)}.`,
                "user"
            );


            input.disabled =
                true;


            button.disabled =
                true;


            await checkAvailability(
                date
            );

        }
    );


    bubble.appendChild(title);

    bubble.appendChild(input);

    bubble.appendChild(button);


    message.appendChild(bubble);


    chat.appendChild(message);


    scrollChat();
}


// ============================================================
// RESERVA VISUAL
// ============================================================
// NO USA GEMINI
// ============================================================

async function showReservationForm(
    preselectedDate = "",
    preselectedTime = "",
    preselectedService = ""
) {

    const message =
        document.createElement("div");


    message.className =
        "message bot";


    const bubble =
        document.createElement("div");


    bubble.className =
        "bubble reservation-form";


    const title =
        document.createElement("div");


    title.className =
        "availability-title";


    title.textContent =
        "Reservar turno";


    const subtitle =
        document.createElement("p");


    subtitle.className =
        "appointment-subtitle";


    subtitle.textContent =
        "Completá los datos para confirmar tu turno.";


    // ========================================================
    // SERVICIO
    // ========================================================

    const serviceLabel =
        document.createElement("label");


    serviceLabel.textContent =
        "Servicio";


    const serviceSelect =
        document.createElement("select");


    serviceSelect.className =
        "reservation-select";


    // ========================================================
    // FECHA
    // ========================================================

    const dateLabel =
        document.createElement("label");


    dateLabel.textContent =
        "Fecha";


    const dateInput =
        document.createElement("input");


    dateInput.type =
        "date";


    dateInput.min =
        getArgentinaDate();


    dateInput.value =
        preselectedDate ||
        getArgentinaDate();


    // ========================================================
    // HORA
    // ========================================================

    const timeLabel =
        document.createElement("label");


    timeLabel.textContent =
        "Horario";


    const timeSelect =
        document.createElement("select");


    timeSelect.className =
        "reservation-select";


    // ========================================================
    // NOMBRE
    // ========================================================

    const nameLabel =
        document.createElement("label");


     nameLabel.textContent =
    "Nombre y apellido";

const nameInput =
    document.createElement("input");

nameInput.type =
    "text";

nameInput.placeholder =
    "Tu nombre y apellido";

nameInput.autocomplete =
    "name";

nameInput.required =
    true;

    // ========================================================
    // TELÉFONO
    // ========================================================

   const phoneLabel =
    document.createElement("label");

phoneLabel.textContent =
    "Teléfono";

const phoneInput =
    document.createElement("input");

phoneInput.type =
    "tel";

phoneInput.placeholder =
    "Tu teléfono";

phoneInput.autocomplete =
    "tel";

phoneInput.required =
    true;
    // ========================================================
    // CONFIRMAR
    // ========================================================

    const confirmButton =
        document.createElement("button");


    confirmButton.className =
        "reservation-submit";


    confirmButton.textContent =
        "Confirmar reserva";


    // ========================================================
    // CERRAR
    // ========================================================

    const closeButton =
        document.createElement("button");


    closeButton.className =
        "reservation-cancel";


    closeButton.textContent =
        "Cerrar";


    // ========================================================
    // CARGAR SERVICIOS
    // ========================================================

    async function loadServices() {

        const data =
            await fetchJSON(
                API.servicios
            );


        if (!data.success) {

            throw new Error(
                data.error ||
                "No se pudieron cargar los servicios."
            );
        }


        serviceSelect.innerHTML =
            "";


        data.servicios.forEach(
            service => {

                const option =
                    document.createElement("option");


                option.value =
                    service.nombre;


                option.textContent =
                    `${service.nombre} — $${Number(
                        service.precio
                    ).toLocaleString("es-AR")}`;


                serviceSelect.appendChild(
                    option
                );

            }
        );


        // Si detectamos un servicio en el texto,
        // lo seleccionamos automáticamente.

        if (
            preselectedService
        ) {

            const exists =
                Array.from(
                    serviceSelect.options
                ).some(
                    option =>
                        option.value ===
                        preselectedService
                );


            if (exists) {

                serviceSelect.value =
                    preselectedService;

            }
        }
    }


    // ========================================================
    // CARGAR HORARIOS
    // ========================================================
async function loadTimes(
    date,
    preferredTime = ""
) {

    timeSelect.disabled = true;

    timeSelect.innerHTML = "";

    // --------------------------------------------------------
    // OPCIÓN INICIAL
    // --------------------------------------------------------

    const loadingOption =
        document.createElement("option");

    loadingOption.value = "";

    loadingOption.textContent =
        "Consultando horarios...";

    timeSelect.appendChild(
        loadingOption
    );

    // --------------------------------------------------------
    // CONSULTAR API
    // --------------------------------------------------------

    const data =
        await fetchJSON(
            `${API.disponibilidad}/${date}`
        );

    if (!data.success) {

        throw new Error(
            data.error ||
            "No se pudo consultar la disponibilidad."
        );
    }

    const times =
        data.horarios_disponibles || [];

    // --------------------------------------------------------
    // LIMPIAR SELECT
    // --------------------------------------------------------

    timeSelect.innerHTML = "";

    // --------------------------------------------------------
    // SIN HORARIOS
    // --------------------------------------------------------

   if (
    times.length === 0
) {

    const option =
        document.createElement("option");

    option.value = "";

    option.textContent =
        "No hay horarios disponibles";

    timeSelect.appendChild(
        option
    );

    timeSelect.disabled =
        false;


    const dayName =
        getDayName(date);


    if (dayName === "domingo") {

        addMessage(
            "El domingo estamos cerrados. " +
            "Atendemos de lunes a sábado de 09:00 a 13:00 y de 15:00 a 20:00. " +
            "Por favor, elegí otro día.",
            "bot"
        );

    } else {

        addMessage(
            `No quedan horarios disponibles para el ${dayName} ${formatDate(date)}. Por favor, elegí otra fecha.`,
            "bot"
        );
    }

    return;
}

    // --------------------------------------------------------
    // OPCIÓN VACÍA
    // --------------------------------------------------------

    const emptyOption =
        document.createElement("option");

    emptyOption.value = "";

    emptyOption.textContent =
        "Elegí un horario";

    timeSelect.appendChild(
        emptyOption
    );

    // --------------------------------------------------------
    // CARGAR HORARIOS
    // --------------------------------------------------------

    times.forEach(
        time => {

            const option =
                document.createElement("option");

            option.value =
                time;

            option.textContent =
                `${time} hs`;

            timeSelect.appendChild(
                option
            );
        }
    );

    // --------------------------------------------------------
    // HORA PEDIDA POR EL CLIENTE
    // --------------------------------------------------------

    if (
        preferredTime
    ) {

        if (
            times.includes(
                preferredTime
            )
        ) {

            // La hora solicitada está disponible.
            timeSelect.value =
                preferredTime;

        } else {

            // La hora solicitada NO está disponible.
            // No elegimos otra silenciosamente.

            timeSelect.value = "";

            addMessage(
    `El horario de las ${preferredTime} hs no está disponible. ` +
    `Atendemos a partir de las 09:00 hs. ` +
    `Podés elegir uno de los horarios disponibles en el formulario.`,
    "bot"
);
        }
    }

    timeSelect.disabled =
        false;
}


    // ========================================================
    // CAMBIAR FECHA
    // ========================================================

    dateInput.addEventListener(
        "change",
        async () => {

            try {

                await loadTimes(
                    dateInput.value
                );

            } catch (error) {

                console.error(error);


                addMessage(
                    error.message ||
                    "No pude consultar la disponibilidad.",
                    "bot"
                );

            }
        }
    );


    // ========================================================
    // CONFIRMAR RESERVA
    // ========================================================

    confirmButton.addEventListener(
        "click",
        async () => {

            const nombre =
                nameInput.value.trim();


            const telefono =
                phoneInput.value.trim();


            const servicio =
                serviceSelect.value;


            const fecha =
                dateInput.value;


            const hora =
                timeSelect.value;


            if (!nombre) {

                addMessage(
                    "Por favor, ingresá tu nombre y apellido.",
                    "bot"
                );

                nameInput.focus();

                return;
            }

            const partesNombre =
                nombre
                    .split(/\s+/)
                    .filter(Boolean);

            if (partesNombre.length < 2) {

                addMessage(
                    "Por favor, ingresá tu nombre y apellido.",
                    "bot"
                );

                nameInput.focus();

                return;
            }

            if (!telefono) {

                addMessage(
                    "Necesito tu número de teléfono para confirmar el turno.",
                    "bot"
                );

                phoneInput.focus();

                return;
            }


            if (!servicio) {

                serviceSelect.focus();

                return;
            }


            if (!fecha) {

                dateInput.focus();

                return;
            }


            if (!hora) {

                timeSelect.focus();

                return;
            }


            confirmButton.disabled =
                true;


            closeButton.disabled =
                true;


            confirmButton.textContent =
                "Reservando...";


            const data =
                await withTyping(
                    "Confirmando tu turno...",
                    () =>
                        fetchJSON(
                            API.reservar,
                            {
                                method: "POST",

                                headers: {
                                    "Content-Type":
                                        "application/json"
                                },

                                body:
                                    JSON.stringify({

                                        nombre,

                                        telefono,

                                        servicio,

                                        fecha,

                                        hora

                                    })
                            }
                        )
                );


            if (!data) {

                confirmButton.disabled =
                    false;


                closeButton.disabled =
                    false;


                confirmButton.textContent =
                    "Confirmar reserva";


                return;
            }


            // =================================================
            // HORARIO OCUPADO
            // =================================================

            if (
                !data.success &&
                data.reason === "occupied"
            ) {

                addMessage(
                    data.error ||
                    "Ese horario ya fue ocupado. Elegí otro horario.",
                    "bot"
                );


                // IMPORTANTE:
                // Esta llamada puede lanzar una excepción.
                // La protegemos para que nunca quede
                // una promesa rechazada sin manejar.

                try {

                    await loadTimes(
                        fecha
                    );

                } catch (error) {

                    console.error(
                        "No se pudieron actualizar los horarios:",
                        error
                    );


                    addMessage(
                        error.message ||
                        "No pude actualizar los horarios disponibles.",
                        "bot"
                    );
                }


                confirmButton.disabled =
                    false;


                closeButton.disabled =
                    false;


                confirmButton.textContent =
                    "Confirmar reserva";


                return;
            }


            // =================================================
            // OTRO ERROR
            // =================================================

            if (!data.success) {

                addMessage(
                    data.error ||
                    "No se pudo realizar la reserva.",
                    "bot"
                );


                confirmButton.disabled =
                    false;


                closeButton.disabled =
                    false;


                confirmButton.textContent =
                    "Confirmar reserva";


                return;
            }


            // =================================================
            // RESERVA CORRECTA
            // =================================================

            message.remove();


            addMessage(
                `¡Listo, ${nombre}! Tu turno quedó reservado correctamente.\n\n` +

                `✂ ${servicio}\n` +

                `📅 ${getDayName(fecha)} ${formatDate(fecha)}\n` +

                `🕐 ${hora} hs\n\n` +

                `¡Te esperamos en El Corte!`,

                "bot"
            );
        }
    );


    // ========================================================
    // CERRAR
    // ========================================================

    closeButton.addEventListener(
        "click",
        () => {

            message.remove();

        }
    );


    // ========================================================
    // CONSTRUIR FORMULARIO
    // ========================================================

    bubble.appendChild(title);

    bubble.appendChild(subtitle);

    bubble.appendChild(serviceLabel);

    bubble.appendChild(serviceSelect);

    bubble.appendChild(dateLabel);

    bubble.appendChild(dateInput);

    bubble.appendChild(timeLabel);

    bubble.appendChild(timeSelect);

    bubble.appendChild(nameLabel);

    bubble.appendChild(nameInput);

    bubble.appendChild(phoneLabel);

    bubble.appendChild(phoneInput);

    bubble.appendChild(confirmButton);

    bubble.appendChild(closeButton);


    message.appendChild(bubble);

    chat.appendChild(message);


    scrollChat();


    // ========================================================
    // CARGAR DATOS INICIALES
    // ========================================================

    try {

        await loadServices();


        await loadTimes(
            dateInput.value,
            preselectedTime
        );


        nameInput.focus();

    } catch (error) {

        console.error(error);


        message.remove();


        addMessage(
            error.message ||
            "No pude preparar el formulario de reserva.",
            "bot"
        );
    }
}


// ============================================================
// MIS TURNOS
// ============================================================

function showMyAppointments() {

    const message =
        document.createElement("div");


    message.className =
        "message bot";


    const bubble =
        document.createElement("div");


    bubble.className =
        "bubble appointment-search";


    const title =
        document.createElement("div");


    title.className =
        "availability-title";


    title.textContent =
        "Consultá tus turnos";


    const subtitle =
        document.createElement("p");


    subtitle.className =
        "appointment-subtitle";


    subtitle.textContent =
        "Ingresá tu nombre y teléfono para buscar tus reservas.";


    const input =
        document.createElement("input");


    input.type =
        "text";


    input.placeholder =
        "Tu nombre";


    input.className =
        "appointment-name-input";

    const phoneInput = document.createElement("input");
    phoneInput.type = "tel";
    phoneInput.placeholder = "Tu teléfono";
    phoneInput.className = "appointment-name-input";


    const button =
        document.createElement("button");


    button.className =
        "appointment-search-button";


    button.textContent =
        "Buscar mis turnos";


    async function search() {

        const name =
            input.value.trim();

        const phone = phoneInput.value.trim();


        if (!name || !phone) {

            input.focus();

            return;
        }


        input.disabled =
            true;


        button.disabled =
            true;


        button.textContent =
            "Buscando...";


        addMessage(
            `Quiero consultar mis turnos. Soy ${name}.`,
            "user"
        );


        const loaded = await loadCustomerAppointments(name, phone);


        if (!loaded) {

            input.disabled =
                false;


            button.disabled =
                false;


            button.textContent =
                "Buscar mis turnos";

        }
    }


    button.addEventListener(
        "click",
        search
    );


    input.addEventListener(
        "keydown",
        event => {

            if (
                event.key === "Enter"
            ) {

                event.preventDefault();

                search();
            }

        }
    );


    bubble.appendChild(title);

    bubble.appendChild(subtitle);

    bubble.appendChild(input);

    bubble.appendChild(phoneInput);

    bubble.appendChild(button);


    message.appendChild(bubble);

    chat.appendChild(message);


    scrollChat();


    input.focus();
}


// ============================================================
// CARGAR TURNOS
// ============================================================

async function loadCustomerAppointments(
    name,
    phone
) {

    const data =
        await withTyping(
            "Buscando tus turnos...",
            () =>
                fetchJSON(
                    `${API.turnos}?nombre=${encodeURIComponent(name)}&telefono=${encodeURIComponent(phone)}`
                )
        );


    if (!data) {

        return false;
    }


    if (!data.success) {

        addMessage(
            data.error ||
            "No pude consultar tus turnos.",
            "bot"
        );


        return false;
    }


    const appointments =
        data.turnos || [];


    if (
        appointments.length === 0
    ) {

        addMessage(
            "⚠️ No encontramos turnos con esos datos. Verificá que el nombre y el teléfono estén escritos correctamente e intentá nuevamente.",
            "bot"
        );


        return false;
    }


    const message =
        document.createElement("div");


    message.className =
        "message bot";


    const container =
        document.createElement("div");


    container.className =
        "appointment-results";


    const title =
        document.createElement("div");


    title.className =
        "appointment-results-title";


    title.textContent =
        `Turnos de ${name}`;


    container.appendChild(title);


    appointments.forEach(
        turno => {

            const card =
                createAppointmentCard(
                    turno,
                    name,
                    phone
                );


            container.appendChild(card);

        }
    );


    message.appendChild(
        container
    );


    chat.appendChild(message);


    scrollChat();


    return true;
}


// ============================================================
// TARJETA DE TURNO
// ============================================================

function createAppointmentCard(
    turno,
    name,
    phone
) {

    const card =
        document.createElement("div");


    card.className =
        "appointment-card";


    card.dataset.appointmentId =
        String(turno.id);


    const service =
        document.createElement("div");


    service.className =
        "appointment-service";


    service.textContent =
        `✂ ${turno.service}`;


    const date =
        document.createElement("div");


    date.className =
        "appointment-detail";


    date.textContent =
        `📅 ${getDayName(turno.appointment_date)} ${formatDate(turno.appointment_date)}`;


    const time =
        document.createElement("div");


    time.className =
        "appointment-detail";


    time.textContent =
        `🕐 ${turno.appointment_time} hs`;


    const status =
        document.createElement("div");


    status.className =
        "appointment-status";


    status.textContent =
        "Confirmado";


    const actions =
        document.createElement("div");


    actions.className =
        "appointment-actions";


    const rescheduleButton =
        document.createElement("button");


    rescheduleButton.className =
        "appointment-reschedule";


    rescheduleButton.textContent =
        "Reprogramar";


    rescheduleButton.addEventListener(
        "click",
        () => {

            showRescheduleOptions(
                turno,
                name,
                phone,
                card
            );

        }
    );


    const cancelButton =
        document.createElement("button");


    cancelButton.className =
        "appointment-cancel";


    cancelButton.textContent =
        "Cancelar";


    cancelButton.addEventListener(
        "click",
        () => {

            showCancelConfirmation(
                turno,
                name,
                phone,
                card
            );

        }
    );


    actions.appendChild(
        rescheduleButton
    );


    actions.appendChild(
        cancelButton
    );


    card.appendChild(service);

    card.appendChild(date);

    card.appendChild(time);

    card.appendChild(status);

    card.appendChild(actions);


    return card;
}


// ============================================================
// CONFIRMACIÓN DE CANCELACIÓN
// ============================================================

function showCancelConfirmation(
    turno,
    name,
    phone,
    card
) {

    const message =
        document.createElement("div");


    message.className =
        "message bot";


    const bubble =
        document.createElement("div");


    bubble.className =
        "bubble cancel-confirmation";


    const title =
        document.createElement("div");


    title.className =
        "availability-title";


    title.textContent =
        "¿Querés cancelar este turno?";


    const details =
        document.createElement("p");


    details.className =
        "appointment-subtitle";


    details.textContent =
        `${turno.service} · ${formatDate(turno.appointment_date)} · ${turno.appointment_time} hs`;


    const actions =
        document.createElement("div");


    actions.className =
        "appointment-actions";


    const confirmButton =
        document.createElement("button");


    confirmButton.className =
        "appointment-cancel";


    confirmButton.textContent =
        "Sí, cancelar";


    const keepButton =
        document.createElement("button");


    keepButton.className =
        "appointment-reschedule";


    keepButton.textContent =
        "No";


    confirmButton.addEventListener(
        "click",
        async () => {

            confirmButton.disabled =
                true;


            keepButton.disabled =
                true;


            await cancelAppointment(
                turno,
                name,
                phone,
                card,
                message
            );

        }
    );


    keepButton.addEventListener(
        "click",
        () => {

            message.remove();

        }
    );


    actions.appendChild(
        confirmButton
    );


    actions.appendChild(
        keepButton
    );


    bubble.appendChild(title);

    bubble.appendChild(details);

    bubble.appendChild(actions);


    message.appendChild(bubble);

    chat.appendChild(message);


    scrollChat();
}


// ============================================================
// CANCELAR TURNO
// ============================================================
// NO USA GEMINI
// ============================================================

async function cancelAppointment(
    turno,
    name,
    phone,
    card,
    confirmationMessage
) {

    const data =
        await withTyping(
            "Cancelando tu turno...",
            () =>
                fetchJSON(
                    API.cancelar,
                    {
                        method: "POST",

                        headers: {
                            "Content-Type":
                                "application/json"
                        },

                        body:
                            JSON.stringify({
                                appointment_id:
                                    turno.id,

                                telefono: phone
                            })
                    }
                )
        );


    if (!data) {

        return;
    }


    if (!data.success) {

        addMessage(
            data.error ||
            "No pude cancelar el turno.",
            "bot"
        );


        return;
    }


    if (card) {

        card.remove();

    }


    if (confirmationMessage) {

        confirmationMessage.remove();

    }


    addMessage(
        `Listo, ${name}. Tu turno del ${formatDate(turno.appointment_date)} a las ${turno.appointment_time} hs fue cancelado correctamente. El horario quedó nuevamente disponible.`,
        "bot"
    );
}


// ============================================================
// REPROGRAMAR - FECHA
// ============================================================

function showRescheduleOptions(
    turno,
    name,
    phone,
    card
) {

    const message =
        document.createElement("div");


    message.className =
        "message bot";


    const bubble =
        document.createElement("div");


    bubble.className =
        "bubble date-picker";


    const title =
        document.createElement("div");


    title.className =
        "availability-title";


    title.textContent =
        "Elegí la nueva fecha";


    const current =
        document.createElement("p");


    current.className =
        "appointment-subtitle";


    current.textContent =
        `Turno actual: ${formatDate(turno.appointment_date)} · ${turno.appointment_time} hs`;


    const input =
        document.createElement("input");


    input.type =
        "date";


    input.min =
        getArgentinaDate();


    const button =
        document.createElement("button");


    button.textContent =
        "Ver horarios disponibles";


    button.addEventListener(
        "click",
        async () => {

            if (!input.value) {

                input.focus();

                return;
            }


            input.disabled =
                true;


            button.disabled =
                true;


            await showRescheduleAvailability(
                turno,
                name,
                phone,
                card,
                input.value,
                message
            );

        }
    );


    bubble.appendChild(title);

    bubble.appendChild(current);

    bubble.appendChild(input);

    bubble.appendChild(button);


    message.appendChild(bubble);


    chat.appendChild(message);


    scrollChat();
}


// ============================================================
// HORARIOS PARA REPROGRAMAR
// ============================================================

async function showRescheduleAvailability(
    turno,
    name,
    phone,
    card,
    newDate,
    previousMessage
) {

    const data =
        await withTyping(
            "Consultando horarios...",
            () =>
                fetchJSON(
                    `${API.disponibilidad}/${newDate}`
                )
        );


    if (!data) {

        return;
    }


    if (!data.success) {

        addMessage(
            data.error ||
            "No pude consultar los horarios.",
            "bot"
        );


        return;
    }


    const times =
        data.horarios_disponibles || [];


    if (
        times.length === 0
    ) {

        addMessage(
            `No hay horarios disponibles para el ${formatDate(newDate)}.`,
            "bot"
        );


        return;
    }


    const message =
        document.createElement("div");


    message.className =
        "message bot";


    const container =
        document.createElement("div");


    container.className =
        "availability-result";


    const title =
        document.createElement("div");


    title.className =
        "availability-title";


    title.textContent =
        `Elegí un horario · ${formatDate(newDate)}`;


    const timeGrid =
        document.createElement("div");


    timeGrid.className =
        "time-grid";


    times.forEach(
        time => {

            const button =
                document.createElement("button");


            button.className =
                "time-button";


            button.textContent =
                `${time} hs`;


            button.addEventListener(
                "click",
                async () => {

                    disableButtons(
                        timeGrid
                    );


                    await rescheduleAppointment(
                        turno,
                        name,
                        phone,
                        card,
                        newDate,
                        time,
                        message,
                        previousMessage
                    );

                }
            );


            timeGrid.appendChild(button);

        }
    );


    container.appendChild(title);

    container.appendChild(timeGrid);


    message.appendChild(container);

    chat.appendChild(message);


    scrollChat();
}


// ============================================================
// REPROGRAMAR TURNO
// ============================================================

async function rescheduleAppointment(
    turno,
    name,
    phone,
    card,
    newDate,
    newTime,
    selectionMessage,
    previousMessage
) {

    const data =
        await withTyping(
            "Reprogramando tu turno...",
            () =>
                fetchJSON(
                    API.reprogramar,
                    {
                        method: "POST",

                        headers: {
                            "Content-Type":
                                "application/json"
                        },

                        body:
                            JSON.stringify({

                                appointment_id:
                                    turno.id,

                                nueva_fecha:
                                    newDate,

                                nueva_hora:
                                    newTime,

                                telefono: phone

                            })
                    }
                )
        );


    if (!data) {

        return;
    }


    if (!data.success) {

        if (
            data.reason ===
            "occupied"
        ) {

            addMessage(
                "Ese horario acaba de ser ocupado. Elegí otro horario.",
                "bot"
            );


            return;
        }


        addMessage(
            data.error ||
            "No pude reprogramar el turno.",
            "bot"
        );


        return;
    }


    turno.appointment_date =
        newDate;


    turno.appointment_time =
        newTime;


    if (card) {

        updateAppointmentCard(
            card,
            turno
        );
    }


    if (selectionMessage) {

        selectionMessage.remove();

    }


    if (previousMessage) {

        previousMessage.remove();

    }


    addMessage(
        `Listo, ${name}. Tu turno fue reprogramado para el ${getDayName(newDate)} ${formatDate(newDate)} a las ${newTime} hs.`,
        "bot"
    );
}


// ============================================================
// ACTUALIZAR TARJETA
// ============================================================

function updateAppointmentCard(
    card,
    turno
) {

    const details =
        card.querySelectorAll(
            ".appointment-detail"
        );


    if (
        details.length >= 2
    ) {

        details[0].textContent =
            `📅 ${getDayName(turno.appointment_date)} ${formatDate(turno.appointment_date)}`;


        details[1].textContent =
            `🕐 ${turno.appointment_time} hs`;
    }
}


// ============================================================
// CHAT LIBRE / FALLBACK IA
// ============================================================

async function sendMessage(
    customMessage = null
) {

    const message =
        customMessage ||
        messageInput.value.trim();


    if (!message) {

        return;
    }


    // --------------------------------------------------------
    // MOSTRAR MENSAJE
    // --------------------------------------------------------

    addMessage(
        message,
        "user"
    );


    messageInput.value =
        "";


    // --------------------------------------------------------
    // PRIMERO: INTENTAR RESOLVER SIN IA
    // --------------------------------------------------------

    const handledLocally =
        await handleLocalIntent(
            message
        );


    // --------------------------------------------------------
    // SI FUE RESUELTO LOCALMENTE,
    // NO LLAMAMOS A GEMINI
    // --------------------------------------------------------

    if (
        handledLocally
    ) {

        return;
    }


    // --------------------------------------------------------
    // FALLBACK A GEMINI
    // --------------------------------------------------------

    sendButton.disabled =
        true;


    messageInput.disabled =
        true;


    const history =
        conversation.slice(
            -MAX_HISTORY_MESSAGES
        );


    const data =
        await withTyping(
            "Pensando...",
            () =>
                fetchJSON(
                    API.chat,
                    {
                        method: "POST",

                        headers: {
                            "Content-Type":
                                "application/json"
                        },

                        body:
                            JSON.stringify({

                                message:
                                    message,

                                conversation:
                                    history
                            })
                    }
                )
        );


    if (!data) {

        sendButton.disabled =
            false;


        messageInput.disabled =
            false;


        messageInput.focus();


        return;
    }


    if (!data.success) {

        console.error(
            data.error
        );


        addMessage(
            data.error ||
            "Ocurrió un problema al procesar tu consulta.",
            "bot"
        );


        sendButton.disabled =
            false;


        messageInput.disabled =
            false;


        messageInput.focus();


        return;
    }


    addMessage(
        data.response,
        "bot"
    );


    // --------------------------------------------------------
    // HISTORIAL
    // --------------------------------------------------------

    conversation.push({

        role:
            "user",

        content:
            message
    });


    conversation.push({

        role:
            "assistant",

        content:
            data.response
    });


    if (
        conversation.length >
        MAX_HISTORY_MESSAGES
    ) {

        conversation =
            conversation.slice(
                -MAX_HISTORY_MESSAGES
            );
    }


    // --------------------------------------------------------
    // DESBLOQUEAR INPUT
    // --------------------------------------------------------

    sendButton.disabled =
        false;


    messageInput.disabled =
        false;


    messageInput.focus();
}


// ============================================================
// BOTONES RÁPIDOS
// ============================================================

document
    .querySelectorAll(
        ".quick-action"
    )
    .forEach(
        button => {

            button.addEventListener(
                "click",
                async function() {

                    const action =
                        this.dataset.message;


                    // ------------------------------------------------
                    // SERVICIOS
                    // ------------------------------------------------

                    if (
                        action ===
                        "¿Qué servicios tienen y cuánto cuestan?"
                    ) {

                        addMessage(
                            action,
                            "user"
                        );


                        await showServices();


                        return;
                    }


                    // ------------------------------------------------
                    // DISPONIBILIDAD
                    // ------------------------------------------------

                    if (
                        action ===
                        "¿Qué horarios hay disponibles?"
                    ) {

                        addMessage(
                            action,
                            "user"
                        );


                        showAvailabilityOptions();


                        return;
                    }


                    // ------------------------------------------------
                    // RESERVAR
                    // ------------------------------------------------

                    if (
                        action ===
                        "Quiero reservar un turno"
                    ) {

                        addMessage(
                            action,
                            "user"
                        );


                        await showReservationForm();


                        return;
                    }


                    // ------------------------------------------------
                    // MIS TURNOS
                    // ------------------------------------------------

                    if (
                        action ===
                        "Quiero consultar mis turnos"
                    ) {

                        addMessage(
                            action,
                            "user"
                        );


                        showMyAppointments();


                        return;
                    }

                }
            );

        }
    );


// ============================================================
// BOTÓN ENVIAR
// ============================================================

sendButton.addEventListener(
    "click",
    () => {

        sendMessage();

    }
);


// ============================================================
// ENTER
// ============================================================

messageInput.addEventListener(
    "keydown",
    event => {

        if (
            event.key === "Enter"
        ) {

            event.preventDefault();

            sendMessage();

        }
    }
);


// ============================================================
// INICIO
// ============================================================

messageInput.focus();
