(function() {
    'use strict';

    const form = window.formData = {
        servicio: null,
        fecha: null,
        hora: null,
        resourceId: null,
        nombre: '',
        telefono: '',
        email: ''
    };

    const slug = typeof BUSINESS_SLUG !== 'undefined' ? BUSINESS_SLUG : null;
    const isRoot = window.location.pathname === '/' || window.location.pathname.match(/^\/b\/[^\/]*$/) === null;

    function showError(message) {
        const banner = document.getElementById('errorBanner');
        const msg = document.getElementById('errorMessage');
        if (banner && msg) {
            msg.textContent = message;
            banner.classList.add('show');
        }
    }

    function clearError() {
        const banner = document.getElementById('errorBanner');
        if (banner) {
            banner.classList.remove('show');
        }
    }

    function showStep(step) {
        document.querySelectorAll('.wizard-step').forEach(function(el) {
            el.classList.remove('active');
            el.style.display = 'none';
        });
        const target = document.getElementById('step' + step);
        if (target) {
            target.classList.add('active');
            target.style.display = 'block';
        }

        document.querySelectorAll('.progress-dot').forEach(function(dot) {
            dot.classList.remove('active');
        });
        document.querySelectorAll('.progress-dot[data-step]').forEach(function(dot) {
            if (parseInt(dot.getAttribute('data-step')) <= step) {
                dot.classList.add('active');
            }
        });

        const backButton = document.getElementById('backButton');
        if (backButton) {
            if (step === 1) {
                backButton.style.display = 'none';
            } else {
                backButton.style.display = 'flex';
            }
        }
    }

    function nextStep(step) {
        if (step === 2) {
            if (!form.servicio) {
                showError('Seleccioná un servicio para continuar.');
                return;
            }
        }
        clearError();
        if (step === 2) {
            loadAvailability();
        }
        if (step === 4) {
            updateConfirmation();
        }
        showStep(step);
    }

    function prevStep(step) {
        clearError();
        showStep(step);
    }

    function goBack() {
        const current = parseInt(getCurrentStep()) || 1;
        if (current > 1) {
            prevStep(current - 1);
        }
    }

    function getCurrentStep() {
        const active = document.querySelector('.wizard-step.active');
        if (active) {
            return active.getAttribute('data-step');
        }
        return '1';
    }

    function selectService(serviceName) {
        clearError();
        form.servicio = serviceName;
        form.fecha = null;
        form.hora = null;
        form.resourceId = null;

        document.querySelectorAll('.service-option').forEach(function(el) {
            el.classList.remove('selected');
        });
        const selected = document.querySelector('.service-option[data-service="' + escapeCss(serviceName) + '"]');
        if (selected) {
            selected.classList.add('selected');
            selected.setAttribute('aria-selected', 'true');
        }

        const nameSpan = document.getElementById('selectedServiceName');
        if (nameSpan) {
            nameSpan.textContent = serviceName;
        }

        const nextBtn = document.getElementById('nextToStep2');
        if (nextBtn) {
            nextBtn.disabled = false;
        }
    }

    function escapeCss(str) {
        return str.replace(/"/g, '\\"').replace(/\\/g, '\\\\');
    }

    function updateConfirmation() {
        const serviceEl = document.getElementById('confirmService');
        const dateEl = document.getElementById('confirmDate');
        const timeEl = document.getElementById('confirmTime');
        const nameEl = document.getElementById('confirmName');
        const phoneEl = document.getElementById('confirmPhone');
        const emailEl = document.getElementById('confirmEmail');

        if (serviceEl) serviceEl.textContent = form.servicio || '-';
        if (dateEl) dateEl.textContent = formatDate(form.fecha) || '-';
        if (timeEl) timeEl.textContent = form.hora || '-';
        if (nameEl) nameEl.textContent = form.nombre || '-';
        if (phoneEl) phoneEl.textContent = form.telefono || '-';
        if (emailEl) emailEl.textContent = form.email || '-';

        const formServicio = document.getElementById('formServicio');
        const formFecha = document.getElementById('formFecha');
        const formHora = document.getElementById('formHora');
        const formResourceId = document.getElementById('formResourceId');

        if (formServicio) formServicio.value = form.servicio || '';
        if (formFecha) formFecha.value = form.fecha || '';
        if (formHora) formHora.value = form.hora || '';
        if (formResourceId) formResourceId.value = form.resourceId || '';
    }

    function formatDate(dateStr) {
        if (!dateStr) return '';
        const parts = dateStr.split('-');
        if (parts.length !== 3) return dateStr;
        const day = parts[2];
        const month = parts[1];
        const year = parts[0];
        const monthNames = [
            'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
            'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre'
        ];
        return parseInt(day) + ' de ' + monthNames[parseInt(month) - 1] + ' de ' + year;
    }

    function loadAvailability() {
        if (!form.fecha) {
            return;
        }

        const loading = document.getElementById('timeLoading');
        const timeGrid = document.getElementById('timeGrid');

        if (loading) loading.style.display = 'block';
        if (timeGrid) timeGrid.style.display = 'none';

        const apiBase = slug ? '/b/' + slug : '';
        const url = apiBase + '/api/disponibilidad/' + form.fecha;

        fetch(url + '?servicio=' + encodeURIComponent(form.servicio || ''))
            .then(function(response) {
                if (response.status === 429) {
                    throw new Error('RATE_LIMITED');
                }
                if (!response.ok) {
                    throw new Error('Error al cargar disponibilidad');
                }
                return response.json();
            })
            .then(function(data) {
                if (loading) loading.style.display = 'none';
                if (timeGrid) timeGrid.style.display = 'grid';

                if (data.success && data.horarios_disponibles) {
                    renderTimeSlots(data.horarios_disponibles);
                } else {
                    if (data.code === 'RATE_LIMITED') {
                        showError('Demasiadas solicitudes. Esperá un momento antes de consultar horarios nuevamente.');
                    } else {
                        showError(data.error || 'No se pudieron cargar los horarios disponibles.');
                    }
                }
            })
            .catch(function(error) {
                if (loading) loading.style.display = 'none';
                if (timeGrid) timeGrid.style.display = 'block';
                if (error.message === 'RATE_LIMITED') {
                    showError('Demasiadas solicitudes. Esperá un momento antes de consultar horarios nuevamente.');
                } else {
                    showError('Error al cargar los horarios: ' + error.message);
                }
            });
    }

    function renderTimeSlots(slots) {
        const grid = document.getElementById('timeGrid');
        if (!grid) return;

        grid.innerHTML = '';

        if (!slots || slots.length === 0) {
            grid.innerHTML = '<div style="text-align: center; padding: 24px; color: var(--text-muted);">No hay horarios disponibles para esta fecha</div>';
            return;
        }

        slots.forEach(function(slot) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'time-option';
            btn.textContent = slot;
            btn.onclick = function() {
                selectTimeSlot(slot);
            };
            grid.appendChild(btn);
        });
    }

    function selectTimeSlot(timeStr) {
        clearError();
        form.hora = timeStr;

        document.querySelectorAll('.time-option').forEach(function(el) {
            el.classList.remove('selected');
        });
        const selected = Array.from(document.querySelectorAll('.time-option')).find(function(el) {
            return el.textContent.trim() === timeStr;
        });
        if (selected) {
            selected.classList.add('selected');
        }

        const nextBtn = document.getElementById('nextToStep3');
        if (nextBtn) {
            nextBtn.disabled = false;
        }
    }

    function setupFormValidation() {
        const nombre = document.getElementById('nombre');
        const telefono = document.getElementById('telefono');
        const nextBtn = document.getElementById('nextToStep4');

        if (nombre) {
            nombre.addEventListener('input', function() {
                form.nombre = nombre.value.trim();
                validateForm();
            });
        }

        if (telefono) {
            telefono.addEventListener('input', function() {
                let value = telefono.value.replace(/\D/g, '');
                form.telefono = value;
                telefono.value = value;
                validateForm();
            });
        }

        if (nextBtn) {
            nextBtn.disabled = true;
        }
    }

    function validateForm() {
        const nombre = document.getElementById('nombre');
        const telefono = document.getElementById('telefono');
        const nextBtn = document.getElementById('nextToStep4');

        if (!nombre || !telefono || !nextBtn) return;

        const nombreValid = nombre.value.trim().length >= 2;
        const telefonoValid = telefono.value.trim().length >= 7 && /^\d+$/.test(telefono.value.trim());

        nextBtn.disabled = !(nombreValid && telefonoValid);
    }

    function submitReservation() {
        clearError();
        const confirmBtn = document.getElementById('confirmButton');
        if (confirmBtn) {
            confirmBtn.disabled = true;
            confirmBtn.textContent = 'Reservando...';
        }

        const apiBase = slug ? '/b/' + slug : '';
        const csrfToken = document.querySelector('input[name="csrf_token"]')?.value || '';

        const payload = {
            csrf_token: csrfToken,
            nombre: form.nombre,
            telefono: form.telefono,
            email: form.email,
            servicio: form.servicio,
            fecha: form.fecha,
            hora: form.hora
        };
        if (form.resourceId) {
            payload.resource_id = form.resourceId;
        }

        fetch(apiBase + '/reservar/confirmar', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest'
            },
            body: JSON.stringify(payload),
            credentials: 'same-origin'
        })
            .then(function(response) {
                return response.json();
            })
            .then(function(data) {
                if (confirmBtn) {
                    confirmBtn.disabled = false;
                    confirmBtn.textContent = 'Confirmar reserva';
                }

                if (data.success) {
                    window.location.href = apiBase + '/?reserva=ok';
                } else {
                    if (data.code === 'email_required') {
                        showError(data.error || 'El email es obligatorio para poder enviarte la confirmación del turno.');
                        const emailInput = document.getElementById('email');
                        if (emailInput) {
                            emailInput.focus();
                        }
                    } else {
                        showError(data.error || 'No se pudo reservar el turno. Por favor, intentá nuevamente.');
                    }
                }
            })
            .catch(function(error) {
                if (confirmBtn) {
                    confirmBtn.disabled = false;
                    confirmBtn.textContent = 'Confirmar reserva';
                }
                showError('Error al reservar el turno: ' + error.message);
            });
    }

    function initCalendar() {
        let currentDate = new Date();
        let selectedDate = null;

        const monthYearEl = document.getElementById('currentMonthYear');
        const gridEl = document.getElementById('calendarGrid');

        if (!monthYearEl || !gridEl) return;

        function renderCalendar(date) {
            const year = date.getFullYear();
            const month = date.getMonth();
            const firstDay = new Date(year, month, 1);
            const lastDay = new Date(year, month + 1, 0);
            const startDate = new Date(firstDay);
            startDate.setDate(startDate.getDate() - firstDay.getDay());

            const monthNames = [
                'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
                'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre'
            ];

            monthYearEl.textContent = monthNames[month] + ' ' + year;
            gridEl.innerHTML = '';

            const dayHeaders = ['Dom', 'Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb'];
            dayHeaders.forEach(function(day) {
                const header = document.createElement('div');
                header.className = 'calendar-header';
                header.textContent = day;
                gridEl.appendChild(header);
            });

            const today = new Date();
            today.setHours(0, 0, 0, 0);

            for (let i = 0; i < 42; i++) {
                const day = new Date(startDate);
                day.setDate(day.getDate() + i);
                day.setHours(0, 0, 0, 0);

                const dayEl = document.createElement('div');
                dayEl.className = 'calendar-day';

                const dayMonth = day.getMonth();
                if (dayMonth !== month) {
                    dayEl.classList.add('other-month');
                }

                if (day < today) {
                    dayEl.classList.add('disabled');
                    dayEl.textContent = day.getDate();
                } else {
                    dayEl.textContent = day.getDate();
                    dayEl.onclick = function() {
                        selectDate(day);
                    };

                    if (selectedDate && day.toDateString() === selectedDate.toDateString()) {
                        dayEl.classList.add('selected');
                    }
                }

                gridEl.appendChild(dayEl);
            }
        }

        function selectDate(day) {
            selectedDate = day;
            const year = day.getFullYear();
            const month = String(day.getMonth() + 1).padStart(2, '0');
            const dayNum = String(day.getDate()).padStart(2, '0');
            const dateStr = year + '-' + month + '-' + dayNum;

            form.fecha = dateStr;

            document.querySelectorAll('.calendar-day').forEach(function(el) {
                el.classList.remove('selected');
            });
            const selected = Array.from(gridEl.querySelectorAll('.calendar-day')).find(function(el) {
                return el.textContent.trim() === dayNum && !el.classList.contains('other-month') && !el.classList.contains('disabled');
            });
            if (selected) {
                selected.classList.add('selected');
            }

            loadAvailability();
        }

        document.getElementById('prevMonth').addEventListener('click', function() {
            currentDate.setMonth(currentDate.getMonth() - 1);
            renderCalendar(currentDate);
        });

        document.getElementById('nextMonth').addEventListener('click', function() {
            currentDate.setMonth(currentDate.getMonth() + 1);
            renderCalendar(currentDate);
        });

        renderCalendar(currentDate);

        window.selectDate = selectDate;
    }

    document.addEventListener('DOMContentLoaded', function() {
        setupFormValidation();

        const currentStep = parseInt(typeof CURRENT_STEP !== 'undefined' ? CURRENT_STEP : '1');
        if (currentStep && currentStep > 1) {
            showStep(currentStep);

            if (currentStep === 2) {
                initCalendar();
            }

            if (currentStep === 3) {
                initCalendar();
            }
        }

        if (typeof CURRENT_STEP !== 'undefined' && CURRENT_STEP === 1) {
            const services = document.querySelectorAll('.service-option');
            if (services.length > 0) {
                services[0].click();
            }
        }
    });

    window.nextStep = nextStep;
    window.prevStep = prevStep;
    window.goBack = goBack;
    window.selectService = selectService;
    window.submitReservation = submitReservation;

})();
