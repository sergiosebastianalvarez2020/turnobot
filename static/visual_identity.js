/* ============================================================
   TURNOBOT — CONFIGURADOR DE IDENTIDAD VISUAL
   Sincroniza muestra, HEX, paleta, presets y vista previa.
   No envía ni persiste datos por sí mismo.
   ============================================================ */

(function () {
    "use strict";

    var HEX_RE = /^#[0-9A-Fa-f]{6}$/;

    function clampChannel(value) {
        return Math.max(0, Math.min(255, Math.round(value)));
    }

    function hexToRgb(hex) {
        var cleaned = String(hex || "").replace("#", "");
        if (!HEX_RE.test("#" + cleaned)) {
            return null;
        }
        return [
            parseInt(cleaned.slice(0, 2), 16),
            parseInt(cleaned.slice(2, 4), 16),
            parseInt(cleaned.slice(4, 6), 16),
        ];
    }

    function channelLuminance(channel) {
        var c = clampChannel(channel) / 255;
        return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    }

    function relativeLuminance(hex) {
        var rgb = hexToRgb(hex);
        if (!rgb) {
            return 0;
        }
        return (
            0.2126 * channelLuminance(rgb[0]) +
            0.7152 * channelLuminance(rgb[1]) +
            0.0722 * channelLuminance(rgb[2])
        );
    }

    function contrastRatio(hexA, hexB) {
        var la = relativeLuminance(hexA);
        var lb = relativeLuminance(hexB);
        var lighter = Math.max(la, lb);
        var darker = Math.min(la, lb);
        return (lighter + 0.05) / (darker + 0.05);
    }

    function readableText(background) {
        var lightRatio = contrastRatio("#FFFFFF", background);
        var darkRatio = contrastRatio("#111827", background);
        return lightRatio >= darkRatio ? "#FFFFFF" : "#111827";
    }

    function setSwatch(element, value) {
        if (element) {
            element.style.setProperty("--vi-swatch", value || "#CCCCCC");
        }
    }

    function normalizeHex(value) {
        var raw = String(value || "").trim();
        return raw ? raw.toUpperCase() : "";
    }

    function initEditor(editor) {
        var colorFields = Array.prototype.slice.call(
            editor.querySelectorAll("[data-vi-color-field]")
        );
        if (!colorFields.length) {
            return;
        }

        var preview = editor.querySelector("[data-vi-preview]");
        var fontSelect = editor.querySelector("[data-vi-font]");
        var colorForm = editor.querySelector("[data-vi-color-form]");
        var presetForm = editor.querySelector("[data-vi-preset-form]");
        var presetNote = editor.querySelector("[data-vi-preset-note]");

        function colorField(key) {
            return colorFields.find(function (field) {
                return field.getAttribute("data-key") === key;
            });
        }

        function colorInput(key) {
            var field = colorField(key);
            return field ? field.querySelector('input[type="color"]') : null;
        }

        function hexInput(key) {
            var field = colorField(key);
            return field ? field.querySelector("[data-vi-hex]") : null;
        }

        function fieldValues() {
            var values = {};
            colorFields.forEach(function (field) {
                var key = field.getAttribute("data-key");
                var input = field.querySelector('input[type="color"]');
                if (key && input) {
                    values[key] = normalizeHex(input.value);
                }
            });
            if (fontSelect) {
                values.font_family = fontSelect.value;
            }
            return values;
        }

        function lockedKeys() {
            return colorFields
                .filter(function (field) {
                    var input = field.querySelector('input[type="color"]');
                    return input && input.disabled;
                })
                .map(function (field) {
                    return field.getAttribute("data-key");
                });
        }

        function updatePalette(key, value) {
            var item = editor.querySelector('[data-vi-palette-item="' + key + '"]');
            if (!item) {
                return;
            }
            setSwatch(item.querySelector("[data-vi-palette-swatch]"), value);
            var hex = item.querySelector("[data-vi-palette-hex]");
            if (hex) {
                hex.textContent = value;
            }
        }

        function updateContrastItem(key, foreground, background) {
            var item = editor.querySelector('[data-vi-contrast-pair="' + key + '"]');
            if (!item || !HEX_RE.test(foreground || "") || !HEX_RE.test(background || "")) {
                return;
            }
            var ratio = contrastRatio(foreground, background);
            var passes = ratio >= 4.5;
            item.setAttribute("data-state", passes ? "pass" : "fail");
            var value = item.querySelector("[data-vi-contrast-value]");
            if (value) {
                value.textContent = ratio.toFixed(2) + ":1 · " + (passes ? "Cumple" : "No cumple");
            }
        }

        function updateContrast(values) {
            updateContrastItem(
                "text",
                values.page_text_color,
                values.page_background_color
            );
            updateContrastItem(
                "button",
                values.button_text_color,
                values.button_bg_color
            );
        }

        function updatePreview(values) {
            if (preview) {
                preview.style.setProperty("--vi-primary", values.primary_color || "#1463FF");
                preview.style.setProperty(
                    "--vi-secondary",
                    values.secondary_color || "#0B1B3A"
                );
                preview.style.setProperty(
                    "--vi-bg",
                    values.page_background_color || "#EAF4FF"
                );
                preview.style.setProperty("--vi-text", values.page_text_color || "#102A56");
                preview.style.setProperty(
                    "--vi-button-bg",
                    values.button_bg_color || values.primary_color || "#1463FF"
                );
                preview.style.setProperty(
                    "--vi-button-text",
                    values.button_text_color || "#FFFFFF"
                );
                preview.style.setProperty(
                    "--vi-on-primary",
                    readableText(values.primary_color || "#1463FF")
                );
                preview.style.setProperty(
                    "--vi-on-secondary",
                    readableText(values.secondary_color || "#0B1B3A")
                );
                if (values.font_family) {
                    var safeFont = String(values.font_family).replace(/["\\]/g, "");
                    preview.style.setProperty(
                        "--vi-font",
                        '"' + safeFont + '", system-ui, sans-serif'
                    );
                }
            }
            updateContrast(values);
        }

        function setHexError(field, message) {
            var hex = field.querySelector("[data-vi-hex]");
            var error = field.querySelector("[data-vi-hex-error]");
            if (hex) {
                if (message) {
                    hex.setAttribute("aria-invalid", "true");
                } else {
                    hex.removeAttribute("aria-invalid");
                }
            }
            if (error) {
                error.textContent = message || "";
                error.hidden = !message;
            }
        }

        function applyHex(field) {
            var key = field.getAttribute("data-key");
            var hex = field.querySelector("[data-vi-hex]");
            var input = field.querySelector('input[type="color"]');
            if (!hex || !input || input.disabled) {
                return true;
            }
            var value = normalizeHex(hex.value);
            if (!HEX_RE.test(value)) {
                setHexError(field, "Usá un HEX válido de 6 dígitos (#RRGGBB).");
                return false;
            }
            setHexError(field, "");
            hex.value = value;
            input.value = value;
            setSwatch(field.querySelector("[data-vi-swatch]"), value);
            updatePalette(key, value);
            updatePreview(fieldValues());
            clearPresetNote();
            return true;
        }

        function clearPresetNote() {
            if (presetNote) {
                presetNote.textContent = "";
            }
        }

        function applyPresetPreview(card) {
            var values = {
                primary_color: card.getAttribute("data-primary-color") || "",
                secondary_color: card.getAttribute("data-secondary-color") || "",
                page_background_color:
                    card.getAttribute("data-page-background-color") || "",
                page_text_color: card.getAttribute("data-page-text-color") || "",
                button_bg_color: card.getAttribute("data-button-bg-color") || "",
                button_text_color: card.getAttribute("data-button-text-color") || "",
                font_family: card.getAttribute("data-font-family") || "",
            };

            lockedKeys().forEach(function (key) {
                var input = colorInput(key);
                if (input) {
                    values[key] = normalizeHex(input.value);
                }
            });

            updatePreview(values);
            if (presetNote) {
                var name = card.getAttribute("data-preset-name") || "";
                presetNote.textContent =
                    "Vista previa del preset " +
                    name +
                    ". Presioná “Aplicar preset” para guardarlo.";
            }
        }

        colorFields.forEach(function (field) {
            var key = field.getAttribute("data-key");
            var input = field.querySelector('input[type="color"]');
            var hex = field.querySelector("[data-vi-hex]");
            if (!input || !hex) {
                return;
            }

            setSwatch(field.querySelector("[data-vi-swatch]"), input.value);

            input.addEventListener("input", function () {
                var value = normalizeHex(input.value);
                hex.value = value;
                setHexError(field, "");
                setSwatch(field.querySelector("[data-vi-swatch]"), value);
                updatePalette(key, value);
                updatePreview(fieldValues());
                clearPresetNote();
            });

            hex.addEventListener("input", function () {
                var value = normalizeHex(hex.value);
                if (HEX_RE.test(value)) {
                    setHexError(field, "");
                    input.value = value;
                    setSwatch(field.querySelector("[data-vi-swatch]"), value);
                    updatePalette(key, value);
                    updatePreview(fieldValues());
                    clearPresetNote();
                }
            });

            hex.addEventListener("blur", function () {
                applyHex(field);
            });
        });

        if (fontSelect) {
            fontSelect.addEventListener("change", function () {
                updatePreview(fieldValues());
                clearPresetNote();
            });
        }

        if (presetForm) {
            presetForm.addEventListener("change", function (event) {
                var target = event.target;
                if (!target || target.name !== "theme_preset") {
                    return;
                }
                var card = target.closest("[data-vi-preset]");
                if (card && target.checked) {
                    applyPresetPreview(card);
                }
            });
        }

        if (colorForm) {
            colorForm.addEventListener("submit", function (event) {
                var firstInvalid = null;
                colorFields.forEach(function (field) {
                    var input = field.querySelector('input[type="color"]');
                    if (input && input.disabled) {
                        return;
                    }
                    if (!applyHex(field) && !firstInvalid) {
                        firstInvalid = field;
                    }
                });
                if (firstInvalid) {
                    event.preventDefault();
                    var hex = firstInvalid.querySelector("[data-vi-hex]");
                    if (hex) {
                        hex.focus();
                    }
                }
            });
        }

        updatePreview(fieldValues());
    }

    function boot() {
        Array.prototype.slice
            .call(document.querySelectorAll("[data-vi-editor]"))
            .forEach(initEditor);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
