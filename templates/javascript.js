<script>
// Gemeinsames JavaScript aller Tabs. index.cgi hängt es an jedes Template an und reicht
// es durch HTML::Template; die Sprachtexte kommen als ein JSON-Objekt (LANG_JSON).
// Alle Daten holt es über ajax.cgi.
(function () {
'use strict';

var L = <TMPL_VAR LANG_JSON>;
var PAGE = document.getElementById('kb-page');
var FORM = PAGE ? PAGE.getAttribute('data-form') : '';
var PARAMS = new URLSearchParams(location.search);

// ---- Hilfen ---------------------------------------------------------------------------------
function t(key) { return Object.prototype.hasOwnProperty.call(L, key) ? L[key] : key; }
function tf(key) {
	var args = Array.prototype.slice.call(arguments, 1), i = 0;
	return t(key).replace(/%s/g, function () { return i < args.length ? args[i++] : ''; });
}
function esc(s) {
	return String(s === undefined || s === null ? '' : s).replace(/[&<>"']/g, function (c) {
		return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
	});
}
function $(id) { return document.getElementById(id); }

function ajax(action, params) {
	var body = new URLSearchParams();
	body.append('action', action);
	Object.keys(params || {}).forEach(function (k) {
		var v = params[k];
		if (v !== undefined && v !== null) body.append(k, typeof v === 'boolean' ? (v ? '1' : '0') : String(v));
	});
	return fetch('ajax.cgi', { method: 'POST', body: body, credentials: 'same-origin' })
		.then(function (r) {
			if (!r.ok) throw new Error('HTTP ' + r.status);
			return r.json();
		})
		.catch(function (e) {
			if (e && e.kbHandled) throw e;
			var err = new Error(t('COMMON.AJAX_ERROR') + ' (' + e.message + ')');
			err.kbHandled = true;
			throw err;
		});
}

function showError(msg) {
	var box = $('kb-error');
	if (!box) return;
	box.innerHTML = msg ? '<div class="lb-callout lb-callout-warning kb-note"><span class="lb-callout-title">'
		+ esc(t('COMMON.ERROR')) + '</span> ' + esc(msg) + '</div>' : '';
}

function toast(el, text, isErr) {
	if (!el) return;
	el.textContent = text;
	el.className = 'kb-toast on' + (isErr ? ' err' : '');
	clearTimeout(el._kbT);
	if (!isErr) el._kbT = setTimeout(function () { el.className = 'kb-toast'; }, 3000);
}

function pill(cls, text) { return '<span class="kb-pill ' + cls + '">' + esc(text) + '</span>'; }

function pad(n) { return (n < 10 ? '0' : '') + n; }
function fmtTime(ts) {
	if (!ts) return '–';
	var d = new Date(ts * 1000);
	return pad(d.getHours()) + ':' + pad(d.getMinutes());
}
function fmtDate(ts) {
	if (!ts) return '–';
	var d = new Date(ts * 1000);
	return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' + d.getFullYear();
}
function fmtDateTime(ts) { return ts ? fmtDate(ts) + ' ' + fmtTime(ts) : '–'; }
function ago(ts) {
	if (!ts) return t('COMMON.NEVER');
	var s = Math.max(0, Math.round(Date.now() / 1000 - ts));
	if (s < 120) return tf('COMMON.AGO_S', s);
	if (s < 7200) return tf('COMMON.AGO_MIN', Math.round(s / 60));
	return tf('COMMON.AGO_H', Math.round(s / 3600));
}
function num(v, digits) {
	if (v === null || v === undefined || v === '') return '–';
	return Number(v).toLocaleString('de-DE', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function stateText(code) {
	if (!code) return '–';
	var key = 'STATE.' + String(code).toUpperCase().replace(/[^A-Z]/g, '');
	return Object.prototype.hasOwnProperty.call(L, key) ? L[key] : code;
}
function apiPill(api) {
	if (api === 'ok') return pill('ok', t('COMMON.API'));
	if (!api || api === 'unknown' || api === 'none') return pill('off', t('COMMON.API'));
	return '<span class="kb-pill err" title="' + esc(stateText(api)) + '">' + esc(t('COMMON.API')) + '</span>';
}
function udpPill(udp) {
	if (udp === 'ok') return pill('ok', t('COMMON.UDP'));
	if (!udp || udp === 'unknown') return pill('off', t('COMMON.UDP'));
	return '<span class="kb-pill err" title="' + esc(stateText(udp)) + '">' + esc(t('COMMON.UDP')) + '</span>';
}

function boxSegment(el, boxes, current, onPick) {
	el.innerHTML = boxes.map(function (b) {
		return '<button type="button" data-id="' + esc(b.id) + '"' + (b.id === current ? ' class="on"' : '') + '>'
			+ esc(b.name || b.id) + '</button>';
	}).join('');
	el.onclick = function (ev) {
		var btn = ev.target.closest('button[data-id]');
		if (!btn) return;
		Array.prototype.forEach.call(el.children, function (c) { c.classList.toggle('on', c === btn); });
		onPick(btn.getAttribute('data-id'));
	};
}

// Knopf zur WebUI der Box (neuer Tab). Adresse ist beim Speichern auf Hostname/IP geprüft.
function webuiButton(host) {
	if (!host) return '';
	return '<a class="lb-btn lb-btn-sm" href="http://' + esc(host) + '/" target="_blank" rel="noopener">'
		+ '<i class="pi pi-external-link"></i> ' + esc(t('COMMON.OPEN_WEBUI')) + '</a>';
}

function topicFor(base, serial, op) {
	var sn = serial || '<sn>';
	if (op.method === 'GET' && op.pollable) return base + '/' + sn + '/' + op.topic;
	return base + '/' + sn + '/' + op.topic + '/' + op.method.toLowerCase();
}

// ---- Übersicht ------------------------------------------------------------------------------
function initOverview() {
	var cfg = { mqtt: { base_topic: 'keba' }, boxes: [] };
	function refresh() {
		ajax('getstatus').then(function (s) { showError(''); renderOverview(s, cfg); })
			.catch(function (e) { showError(e.message); });
	}
	ajax('getconfig').then(function (c) { if (c.ok) cfg = c; }).catch(function () {}).then(function () {
		refresh();
		setInterval(refresh, 5000);
	});
	$('ov-restart').onclick = function () { gatewayAction('restart', refresh); };
	$('ov-stop').onclick = function () { gatewayAction('stop', refresh); };
}

function gatewayAction(action, done) {
	var btns = [$('ov-restart'), $('ov-stop')];
	btns.forEach(function (b) { b.disabled = true; });
	ajax(action).then(function (r) {
		if (r.ok) toast($('ov-gw-toast'), t(action === 'stop' ? 'OVERVIEW.STOPPED_TOAST' : 'OVERVIEW.RESTARTED'));
		else toast($('ov-gw-toast'), r.error || t('COMMON.ERROR'), true);
	}).catch(function (e) { toast($('ov-gw-toast'), e.message, true); })
		.then(function () { btns.forEach(function (b) { b.disabled = false; }); done(); });
}

function renderOverview(s, cfg) {
	var gw = s.gateway || {};
	var gwPill = $('ov-gw-pill'), gwDesc = $('ov-gw-desc');
	if (s.running) {
		gwPill.outerHTML = '<span class="kb-pill ok" id="ov-gw-pill">' + esc(t('OVERVIEW.RUNNING')) + '</span>';
		gwDesc.textContent = tf('OVERVIEW.SINCE', fmtDateTime(gw.since));
	} else if (s.watchdog_gaveup) {
		gwPill.outerHTML = '<span class="kb-pill err" id="ov-gw-pill">' + esc(t('OVERVIEW.GAVEUP')) + '</span>';
		gwDesc.textContent = t('OVERVIEW.GAVEUP_DESC');
	} else if (s.stopped_flag) {
		gwPill.outerHTML = '<span class="kb-pill off" id="ov-gw-pill">' + esc(t('OVERVIEW.STOPPED')) + '</span>';
		gwDesc.textContent = t('OVERVIEW.STOPPED_DESC');
	} else {
		gwPill.outerHTML = '<span class="kb-pill err" id="ov-gw-pill">' + esc(t('OVERVIEW.NOT_RUNNING')) + '</span>';
		gwDesc.textContent = t('OVERVIEW.DEAD_DESC');
	}
	$('ov-stop').disabled = !s.running;

	var connected = s.running && gw.mqtt === 'connected';
	$('ov-mqtt-pill').outerHTML = '<span class="kb-pill ' + (connected ? 'ok' : 'err') + '" id="ov-mqtt-pill">'
		+ esc(t(connected ? 'OVERVIEW.CONNECTED' : 'OVERVIEW.DISCONNECTED')) + '</span>';
	$('ov-mqtt-desc').innerHTML = esc(tf('OVERVIEW.MQTT_DESC', '')) + '<span class="kb-mono">' + esc(cfg.mqtt.base_topic) + '</span>';

	var statuses = s.boxes || {};
	var users = {};
	(cfg.boxes || []).forEach(function (b) { users[b.id] = b.username; });
	var ids = Object.keys(statuses);
	var active = ids.filter(function (id) { return statuses[id].active !== false; });
	var complete = active.filter(function (id) {
		var b = statuses[id];
		return b.online !== false && b.api === 'ok' && b.udp === 'ok' && !b.error;
	});
	var boxPill = $('ov-box-pill');
	if (!ids.length) {
		boxPill.outerHTML = '<span class="kb-pill off" id="ov-box-pill">0</span>';
		$('ov-box-desc').textContent = t('COMMON.NO_BOXES');
	} else {
		boxPill.outerHTML = '<span class="kb-pill ' + (complete.length === active.length ? 'ok' : 'warn')
			+ '" id="ov-box-pill">' + esc(tf('OVERVIEW.BOXES_COMPLETE', complete.length, active.length)) + '</span>';
		$('ov-box-desc').textContent = active.map(function (id) {
			var b = statuses[id];
			return (b.name || id) + ': ' + t('COMMON.API') + ' ' + stateText(b.api) + ', ' + t('COMMON.UDP') + ' ' + stateText(b.udp);
		}).join(' · ');
	}

	if (!ids.length && (cfg.boxes || []).length) {
		$('ov-boxes').innerHTML = '<div class="lb-card kb-card"><div class="kb-body kb-muted">' + esc(t('OVERVIEW.NO_STATUS')) + '</div></div>';
		return;
	}
	$('ov-boxes').innerHTML = ids.map(function (id) { return boxCard(id, statuses[id], users[id]); }).join('');
}

function boxCard(id, b, username) {
	var head = '<div class="kb-head"><h3>' + esc(b.name || id)
		+ (b.serial ? ' <span class="kb-mono kb-sub-inline">SN ' + esc(b.serial) + '</span>' : '') + '</h3>';
	if (b.active === false) {
		return '<article class="lb-card kb-card kb-inactive">' + head + pill('off', t('STATE.INACTIVE')) + '</div>'
			+ '<div class="kb-body kb-muted">' + esc(t('OVERVIEW.INACTIVE_DESC')) + '</div></article>';
	}
	head += '<div class="kb-pills">' + (b.online === false ? pill('err', t('STATE.OFFLINE')) : '')
		+ apiPill(b.api) + udpPill(b.udp) + '</div></div>';

	var notes = [];
	if (b.error === 'serial_mismatch') notes.push(t('OVERVIEW.NOTE_SERIAL'));
	else if (b.api === 'api_off') notes.push(t('OVERVIEW.NOTE_API_OFF'));
	else if (b.api === 'auth_failed') notes.push(t('OVERVIEW.NOTE_AUTH'));
	else if (b.api === 'unreachable' && b.udp !== 'ok') notes.push(t('OVERVIEW.NOTE_UNREACHABLE'));
	else if (b.error && b.error !== 'serial_mismatch') notes.push(tf('OVERVIEW.NOTE_ERROR', b.error));
	if (b.udp === 'timeout' && b.api === 'ok') notes.push(t('OVERVIEW.NOTE_UDP'));
	if (b.udp === 'port_busy') notes.push(t('OVERVIEW.NOTE_PORT_BUSY'));
	if (b.missing && b.missing.length) notes.push(tf('OVERVIEW.NOTE_MISSING', b.missing.join(', ')));

	var live = b.live || {};
	var hasLive = Object.keys(live).length > 0;
	var plug = live.plug === true ? t('STATE.PLUGGED') : (live.plug === false ? t('STATE.UNPLUGGED') : '–');
	var liveHtml = '<div class="kb-live">'
		+ '<div><span>' + esc(t('OVERVIEW.LIVE_STATE')) + '</span><b>' + esc(hasLive ? stateText(live.state) : '–') + '</b></div>'
		+ '<div><span>' + esc(t('OVERVIEW.LIVE_POWER')) + '</span><b>' + (hasLive && live.power_kw !== null ? esc(num(live.power_kw, 1)) + ' kW' : '–') + '</b></div>'
		+ '<div><span>' + esc(t('OVERVIEW.LIVE_SESSION')) + '</span><b>' + (hasLive && live.session_kwh !== null && live.session_kwh !== undefined ? esc(num(live.session_kwh, 1)) + ' kWh' : '–') + '</b></div>'
		+ '<div><span>' + esc(t('OVERVIEW.LIVE_PLUG')) + '</span><b>' + esc(plug) + '</b></div></div>';

	function kv(k, v) { return '<dt>' + esc(t(k)) + '</dt><dd>' + v + '</dd>'; }
	var fw = esc(b.firmware || '–');
	if (b.api_version || b.op_count) fw += ' <span class="kb-sub-inline">· ' + esc(tf('OVERVIEW.FW_ENDPOINTS', b.api_version || '?', b.op_count || 0)) + '</span>';
	var login = b.auth === 'ok' ? esc(tf('OVERVIEW.LOGIN_AS', '')) + '<span class="kb-mono">' + esc(username || '') + '</span>' : esc(stateText(b.auth));
	var access = b.auth === 'ok' && b.token_expires
		? esc(tf('OVERVIEW.ACCESS_UNTIL', fmtTime(b.token_expires))) + ' <span class="kb-sub-inline">· ' + esc(t('OVERVIEW.ACCESS_AUTO')) + '</span>' : '–';
	var refresh = b.refresh_expires ? esc(tf('OVERVIEW.ACCESS_UNTIL', fmtDate(b.refresh_expires))) : '–';
	var dl = '<dl class="kb-kv">'
		+ kv('OVERVIEW.MODEL', esc(b.model || '–'))
		+ kv('OVERVIEW.FIRMWARE', fw)
		+ kv('OVERVIEW.ADDRESS', '<span class="kb-mono">' + esc(b.host || '') + '</span>')
		+ kv('OVERVIEW.LOGIN', login)
		+ kv('OVERVIEW.ACCESS', access)
		+ kv('OVERVIEW.REFRESH', refresh)
		+ kv('OVERVIEW.ENDPOINTS', esc(tf('OVERVIEW.ENDPOINTS_V', b.enabled_count || 0, b.op_count || 0)))
		+ kv('OVERVIEW.LAST_POLL', esc(ago(b.last_poll)))
		+ kv('OVERVIEW.LAST_UDP', esc(b.last_udp ? ago(b.last_udp) : t('OVERVIEW.NONE_SINCE_START')))
		+ '</dl>';
	var noteHtml = notes.map(function (n) {
		return '<div class="lb-callout lb-callout-warning kb-note">' + esc(n) + '</div>';
	}).join('');
	var foot = '<div class="kb-foot">'
		+ '<a class="lb-btn lb-btn-sm" href="index.cgi?form=docs&amp;box=' + encodeURIComponent(id) + '"><i class="pi pi-list"></i> ' + esc(t('OVERVIEW.LINK_DOCS')) + '</a>'
		+ '<a class="lb-btn lb-btn-sm" href="index.cgi?form=settings&amp;edit=' + encodeURIComponent(id) + '"><i class="pi pi-pencil"></i> ' + esc(t('OVERVIEW.LINK_EDIT')) + '</a>'
		+ webuiButton(b.host) + '</div>';
	return '<article class="lb-card kb-card">' + head + '<div class="kb-body">' + noteHtml + liveHtml + dl + '</div>' + foot + '</article>';
}

// ---- Einstellungen --------------------------------------------------------------------------
function initSettings() {
	var cfg = { boxes: [] }, statuses = {}, editing = null, deleteArmed = false;

	function load() {
		return Promise.all([ajax('getconfig'), ajax('getstatus').catch(function () { return {}; })])
			.then(function (r) {
				if (!r[0].ok) throw new Error(r[0].error || t('COMMON.ERROR'));
				cfg = r[0];
				statuses = (r[1] && r[1].boxes) || {};
				renderTable();
			}).catch(function (e) { showError(e.message); });
	}

	function renderTable() {
		if (!cfg.boxes.length) {
			$('st-rows').innerHTML = '<tr><td colspan="8" class="kb-muted">' + esc(t('COMMON.NO_BOXES')) + '</td></tr>';
			return;
		}
		$('st-rows').innerHTML = cfg.boxes.map(function (b) {
			var st = statuses[b.id] || {};
			var model = (st.model || '–') + (st.firmware ? ' · ' + st.firmware : '');
			var api = b.active ? (st.api === 'ok' ? pill('ok', t('STATE.OK')) : pill(st.api && st.api !== 'unknown' ? 'err' : 'off', stateText(st.api || 'unknown'))) : '–';
			var udp = b.active ? (st.udp === 'ok' ? pill('ok', t('STATE.OK')) : pill(st.udp && st.udp !== 'unknown' ? 'err' : 'off', stateText(st.udp || 'unknown'))) : '–';
			return '<tr' + (editing && editing.id === b.id ? ' class="kb-sel"' : '') + '>'
				+ '<td><b>' + esc(b.name || b.id) + '</b></td>'
				+ '<td class="kb-mono">' + esc(b.serial || '–') + '</td>'
				+ '<td class="kb-mono">' + esc(b.host) + '</td>'
				+ '<td>' + esc(model) + '</td><td>' + api + '</td><td>' + udp + '</td>'
				+ '<td>' + esc(t(b.active ? 'COMMON.YES' : 'COMMON.NO')) + '</td>'
				+ '<td><div class="kb-actions kb-nw"><button type="button" class="lb-btn lb-btn-sm" data-edit="' + esc(b.id) + '">' + esc(t('SETTINGS.EDIT')) + '</button>'
				+ webuiButton(b.host) + '</div></td></tr>';
		}).join('');
	}

	function openForm(box) {
		editing = box;
		deleteArmed = false;
		$('st-form').hidden = false;
		$('st-form-title').innerHTML = box
			? esc(t('SETTINGS.FORM_EDIT')) + ' <span class="kb-mono kb-sub-inline">' + esc(box.name || box.id) + (box.serial ? ' · SN ' + esc(box.serial) : '') + '</span>'
			: esc(t('SETTINGS.FORM_NEW'));
		$('f-name').value = box ? box.name : '';
		$('f-host').value = box ? box.host : '';
		$('f-user').value = box ? (box.username || 'admin') : 'admin';
		$('f-pw').value = box ? (box.password || '') : '';
		$('f-pw').type = 'password';
		$('f-pw-eye').innerHTML = '<i class="pi pi-eye"></i>';
		$('f-active').checked = box ? !!box.active : true;
		$('f-delete').hidden = !box;
		$('f-delete').querySelector('span').textContent = t('SETTINGS.DELETE');
		$('f-check-wrap').hidden = true;
		$('f-toast').className = 'kb-toast';
		renderTable();
		$('st-form').scrollIntoView({ behavior: 'smooth', block: 'start' });
	}

	function renderChecks(steps) {
		var labels = { login: 'SETTINGS.STEP_LOGIN', serial: 'SETTINGS.STEP_SERIAL', version: 'SETTINGS.STEP_VERSION',
			openapi: 'SETTINGS.STEP_OPENAPI', udp: 'SETTINGS.STEP_UDP' };
		$('f-check-wrap').hidden = false;
		$('f-checks').innerHTML = (steps || []).map(function (s) {
			return '<li class="' + (s.ok ? 'ok' : 'err') + '"><span class="kb-mark"><i class="pi ' + (s.ok ? 'pi-check' : 'pi-times') + '"></i></span>'
				+ '<span>' + esc(t(labels[s.key] || s.key)) + '</span>'
				+ '<span class="kb-mono">' + esc(s.ok ? s.value : s.error) + '</span>'
				+ '<span class="kb-sub-inline">' + esc(s.ms) + ' ms</span></li>';
		}).join('');
	}

	function formData() {
		return { id: editing ? editing.id : '', name: $('f-name').value.trim(), host: $('f-host').value.trim(),
			username: $('f-user').value.trim(), password: $('f-pw').value, active: $('f-active').checked };
	}

	$('st-rows').onclick = function (ev) {
		var btn = ev.target.closest('button[data-edit]');
		if (!btn) return;
		var id = btn.getAttribute('data-edit');
		openForm(cfg.boxes.filter(function (b) { return b.id === id; })[0] || null);
	};
	$('st-add').onclick = function () { openForm(null); };
	$('f-cancel').onclick = function () { editing = null; $('st-form').hidden = true; renderTable(); };
	$('f-pw-eye').addEventListener('click', function (ev) {
		ev.preventDefault();
		var pw = $('f-pw'), show = pw.getAttribute('type') === 'password';
		pw.setAttribute('type', show ? 'text' : 'password');
		$('f-pw-eye').innerHTML = '<i class="pi ' + (show ? 'pi-eye-slash' : 'pi-eye') + '"></i>';
	});
	$('f-check').onclick = function () {
		var d = formData();
		if (!d.host) { toast($('f-toast'), t('SETTINGS.ERR_HOST'), true); return; }
		toast($('f-toast'), t('SETTINGS.CHECKING'));
		this.disabled = true;
		var btn = this;
		ajax('checkbox', { id: d.id, host: d.host, username: d.username, password: d.password }).then(function (r) {
			if (r.error) toast($('f-toast'), r.error, true);
			else { renderChecks(r.steps); $('f-toast').className = 'kb-toast'; }
		}).catch(function (e) { toast($('f-toast'), e.message, true); }).then(function () { btn.disabled = false; });
	};
	$('f-save').onclick = function () {
		var d = formData();
		if (!d.host) { toast($('f-toast'), t('SETTINGS.ERR_HOST'), true); return; }
		if (!d.password) { toast($('f-toast'), t('SETTINGS.ERR_PW'), true); return; }
		toast($('f-toast'), t('SETTINGS.SAVING'));
		var btn = this;
		btn.disabled = true;
		ajax('savebox', d).then(function (r) {
			if (!r.ok) { toast($('f-toast'), r.error || t('COMMON.ERROR'), true); return; }
			// Gespeichert: Formular schließen, Meldung an der Box-Tabelle
			editing = null;
			$('st-form').hidden = true;
			$('f-toast').className = 'kb-toast';
			return load().then(function () {
				toast($('st-toast'), t(r.serial ? 'COMMON.SAVED' : 'SETTINGS.SAVED_NOSERIAL'));
				$('st-rows').closest('section').scrollIntoView({ behavior: 'smooth', block: 'start' });
			});
		}).catch(function (e) { toast($('f-toast'), e.message, true); }).then(function () { btn.disabled = false; });
	};
	$('f-delete').onclick = function () {
		if (!editing) return;
		if (!deleteArmed) {
			deleteArmed = true;
			this.querySelector('span').textContent = t('SETTINGS.DELETE_CONFIRM');
			return;
		}
		ajax('deletebox', { id: editing.id }).then(function (r) {
			if (!r.ok) { toast($('f-toast'), r.error || t('COMMON.ERROR'), true); return; }
			editing = null;
			$('st-form').hidden = true;
			load();
		}).catch(function (e) { toast($('f-toast'), e.message, true); });
	};

	load().then(function () {
		if (PARAMS.get('new')) openForm(null);
		var edit = PARAMS.get('edit');
		if (edit) openForm(cfg.boxes.filter(function (b) { return b.id === edit; })[0] || null);
	});
}

// ---- MQTT -----------------------------------------------------------------------------------
function initMqtt() {
	var cfg = null, statuses = {}, boxId = null, serial = '';

	function renderSchema() {
		var base = cfg.mqtt.base_topic;
		$('m-base-ex').textContent = base + '/<' + t('SETTINGS.COL_SERIAL').toLowerCase() + '>/…';
		var rows = [
			[base + '/gateway', 'out', 'MQTT.S_GATEWAY'],
			[base + '/<sn>/status', 'out', 'MQTT.S_STATUS'],
			[base + '/<sn>/wallbox', 'out', 'MQTT.S_WALLBOX'],
			[base + '/<sn>/<endpunkt>', 'out', 'MQTT.S_ENDPOINT'],
			[base + '/<sn>/<endpunkt>/get', 'in', 'MQTT.S_GET'],
			[base + '/<sn>/<endpunkt>/<methode>', 'in', 'MQTT.S_COMMAND'],
			[base + '/<sn>/<endpunkt>/<methode>/result', 'out', 'MQTT.S_RESULT'],
			[base + '/<sn>/udp/set', 'in', 'MQTT.S_UDPSET'],
			[base + '/<sn>/udp/result', 'out', 'MQTT.S_UDPRESULT'],
			[base + '/<sn>/display/set', 'in', 'MQTT.S_DISPLAY']
		];
		$('m-schema').innerHTML = rows.map(function (r) {
			return '<tr><td class="kb-mono kb-nw">' + esc(r[0]) + '</td><td><span class="kb-dir ' + r[1] + '">'
				+ esc(t(r[1] === 'out' ? 'MQTT.DIR_OUT' : 'MQTT.DIR_IN')) + '</span></td><td>' + esc(t(r[2])) + '</td></tr>';
		}).join('');
	}

	function fillForm() {
		$('m-base').value = cfg.mqtt.base_topic;
		$('m-interval').value = cfg.mqtt.interval;
		$('m-ret').checked = !!cfg.mqtt.retain;
	}

	function methodTag(m) { return '<span class="kb-m ' + esc(m.toLowerCase()) + '">' + esc(m) + '</span>'; }

	function loadOps(id) {
		boxId = id;
		var box = cfg.boxes.filter(function (b) { return b.id === id; })[0] || {};
		serial = box.serial || '';
		$('m-more').href = 'index.cgi?form=docs&box=' + encodeURIComponent(id);
		$('m-ops').innerHTML = '<tr><td colspan="4" class="kb-muted">' + esc(t('COMMON.LOADING')) + '</td></tr>';
		ajax('getoperations', { id: id }).then(function (r) {
			if (!r.ok) {
				$('m-ops').innerHTML = '<tr><td colspan="4" class="kb-muted">' + esc(t('MQTT.NO_OPS')) + '</td></tr>';
				return;
			}
			serial = r.serial || serial;
			renderOps(r.ops || []);
		}).catch(function (e) { showError(e.message); });
	}

	function renderOps(ops) {
		var base = cfg.mqtt.base_topic, rows = [];
		var shown = ops.filter(function (o) { return o.status === 'enabled' || o.status === 'missing'; });
		shown.sort(function (a, b) { return (a.status === b.status ? 0 : a.status === 'enabled' ? -1 : 1) || a.path.localeCompare(b.path); });
		shown.forEach(function (o) {
			var cycle = o.status === 'missing' ? '<span class="kb-tag blk">' + esc(t('MQTT.ORIGIN_MISSING')) + '</span>'
				: o.interval ? esc(o.interval) + ' s' : '<span class="kb-sub-inline">' + esc(t('COMMON.COMMAND')) + '</span>';
			rows.push('<tr><td class="kb-nw">' + methodTag(o.method) + ' <span class="kb-mono">' + esc(o.path) + '</span></td>'
				+ '<td class="kb-mono kb-nw">' + esc(o.topic ? topicFor(base, serial, o) : '–') + '</td><td class="kb-nw">' + cycle + '</td>'
				+ '<td><button type="button" class="kb-x" data-remove="' + esc(o.op) + '" aria-label="' + esc(t('MQTT.REMOVE'))
				+ '" title="' + esc(t('MQTT.REMOVE')) + '"><i class="pi pi-times"></i></button></td></tr>');
		});
		if (!shown.length) rows.push('<tr><td colspan="4" class="kb-muted">' + esc(t('MQTT.NONE_ENABLED')) + '</td></tr>');
		[['udp/set', 'MQTT.UDP_ANY'], ['display/set', 'MQTT.DISPLAY_TEXT']].forEach(function (u) {
			rows.push('<tr><td class="kb-nw">' + methodTag('UDP') + ' <span class="kb-mono">' + esc(t(u[1])) + '</span></td>'
				+ '<td class="kb-mono kb-nw">' + esc(base + '/' + (serial || '<sn>') + '/' + u[0]) + '</td>'
				+ '<td class="kb-sub-inline">' + esc(t('COMMON.COMMAND')) + '</td><td></td></tr>');
		});
		$('m-ops').innerHTML = rows.join('');
	}

	$('m-ops').onclick = function (ev) {
		var btn = ev.target.closest('button[data-remove]');
		if (!btn) return;
		ajax('setop', { id: boxId, op: btn.getAttribute('data-remove'), enabled: 0 }).then(function (r) {
			if (!r.ok) { toast($('m-ops-toast'), r.error || t('COMMON.ERROR'), true); return; }
			loadOps(boxId);
		}).catch(function (e) { toast($('m-ops-toast'), e.message, true); });
	};
	$('m-save').onclick = function () {
		ajax('savemqtt', { base_topic: $('m-base').value.trim(), interval: $('m-interval').value,
			retain: $('m-ret').checked }).then(function (r) {
			if (!r.ok) { toast($('m-toast'), r.error || t('COMMON.ERROR'), true); return; }
			cfg.mqtt = r.mqtt;
			fillForm();
			renderSchema();
			toast($('m-toast'), t('COMMON.SAVED'));
			if (boxId) loadOps(boxId);
		}).catch(function (e) { toast($('m-toast'), e.message, true); });
	};

	ajax('getconfig').then(function (c) {
		if (!c.ok) throw new Error(c.error || t('COMMON.ERROR'));
		cfg = c;
		fillForm();
		renderSchema();
		if (!cfg.boxes.length) {
			$('m-ops').innerHTML = '<tr><td colspan="4" class="kb-muted">' + esc(t('COMMON.NO_BOXES')) + '</td></tr>';
			return;
		}
		var first = PARAMS.get('box') || cfg.boxes[0].id;
		boxSegment($('m-boxes'), cfg.boxes, first, loadOps);
		loadOps(first);
	}).catch(function (e) { showError(e.message); });
}

// ---- Dokumentation --------------------------------------------------------------------------
function initDocs() {
	var cfg = null, boxId = null, data = null, filter = 'all', query = '', open = {};

	function schemaText(s) {
		if (!s) return t('DOCS.D_NONE');
		var txt = JSON.stringify(s, null, 2);
		return txt.length > 6000 ? txt.slice(0, 6000) + '\n…' : txt;
	}

	function noteFor(o) {
		if (o.status === 'blocked') return tf('DOCS.N_BLOCKED', o.block_reason);
		if (o.method === 'GET') return t(o.pollable ? 'DOCS.N_POLL' : 'DOCS.N_GETPARAM');
		if (o.body_schema === undefined) return '';
		return t((o.path_params && o.path_params.length) || (o.query_params && o.query_params.length) ? 'DOCS.N_CMD_ENV' : 'DOCS.N_CMD');
	}

	function matches(o) {
		if (filter !== 'all' && o.status !== filter) return false;
		if (!query) return true;
		var hay = (o.path + ' ' + (o.summary || '') + ' ' + (o.topic || '') + ' ' + (o.description || '') + ' ' + o.method).toLowerCase();
		return hay.indexOf(query) !== -1;
	}

	function renderChips() {
		var counts = { all: data.ops.length, enabled: 0, available: 0, blocked: 0, missing: 0 };
		data.ops.forEach(function (o) { counts[o.status] = (counts[o.status] || 0) + 1; });
		var chips = [['all', 'DOCS.F_ALL'], ['enabled', 'DOCS.F_ENABLED'],
			['available', 'DOCS.F_AVAILABLE'], ['blocked', 'DOCS.F_BLOCKED']];
		if (counts.missing) chips.push(['missing', 'DOCS.F_MISSING']);
		$('d-chips').innerHTML = chips.map(function (c) {
			return '<button type="button" data-filter="' + c[0] + '" class="kb-chip' + (filter === c[0] ? ' on' : '') + '">'
				+ esc(t(c[1])) + ' <span class="kb-cnt">' + counts[c[0]] + '</span></button>';
		}).join('');
	}

	function statusCell(o) {
		if (o.status === 'blocked') return '<span class="kb-tag blk" title="' + esc(o.block_reason) + '">' + esc(t('DOCS.TAG_BLOCKED')) + '</span>';
		var label = t(o.method === 'GET' && o.pollable ? 'DOCS.CHK_POLL' : 'DOCS.CHK_CMD');
		var html = '<label class="kb-chk"><input type="checkbox" data-op="' + esc(o.op) + '"' + (o.status === 'enabled' || o.status === 'missing' ? ' checked' : '')
			+ '> ' + esc(label) + '</label>';
		if (o.status === 'missing') html = '<span class="kb-tag blk">' + esc(t('DOCS.TAG_MISSING')) + '</span> ' + html;
		return html;
	}

	function renderRows() {
		var base = cfg.mqtt.base_topic, groups = {}, order = [];
		data.ops.filter(matches).forEach(function (o) {
			var tag = o.tag || '–';
			if (!groups[tag]) { groups[tag] = []; order.push(tag); }
			groups[tag].push(o);
		});
		if (!order.length) {
			$('d-rows').innerHTML = '<tr><td colspan="5" class="kb-muted">' + esc(t('DOCS.EMPTY')) + '</td></tr>';
			return;
		}
		order.sort();
		var html = [];
		order.forEach(function (tag) {
			html.push('<tr class="kb-group"><td colspan="5">' + esc(tag) + ' <span class="kb-cnt">' + groups[tag].length + '</span></td></tr>');
			groups[tag].forEach(function (o) {
				var isOpen = !!open[o.op];
				html.push('<tr class="kb-row' + (o.status === 'blocked' ? ' kb-blocked' : '') + '">'
					+ '<td><span class="kb-m ' + esc(o.method.toLowerCase()) + '">' + esc(o.method) + '</span></td>'
					+ '<td><button type="button" class="kb-pathbtn" data-open="' + esc(o.op) + '" aria-expanded="' + isOpen + '">'
					+ '<i class="pi ' + (isOpen ? 'pi-chevron-down' : 'pi-chevron-right') + '"></i><span class="kb-mono">' + esc(o.path) + '</span></button></td>'
					+ '<td class="kb-desc">' + esc(o.summary || '') + '</td>'
					+ '<td class="kb-mono kb-topic">' + esc(o.topic ? topicFor(base, data.serial, o) : '–') + '</td>'
					+ '<td class="kb-nw">' + statusCell(o) + '</td></tr>');
				if (isOpen) {
					var params = [].concat((o.path_params || []).map(function (p) { return '{' + p + '} (path)'; }),
						(o.query_params || []).map(function (q) { return q.name + (q.required ? '*' : '') + ' (query)'; }));
					var note = noteFor(o);
					html.push('<tr class="kb-detail"><td></td><td colspan="4"><div class="kb-detail-grid">'
						+ (o.description && o.description !== o.summary ? '<div class="kb-wide"><span class="kb-desc">' + esc(o.description) + '</span></div>' : '')
						+ '<div><b>' + esc(t('DOCS.D_PARAMS')) + '</b><span class="kb-mono">' + esc(params.length ? params.join(', ') : t('DOCS.D_NONE')) + '</span></div>'
						+ (note ? '<div><b>' + esc(t('DOCS.D_NOTE')) + '</b><span class="kb-desc">' + esc(note) + '</span></div>' : '')
						+ '<div><b>' + esc(t('DOCS.D_BODY')) + '</b><pre class="kb-code">' + esc(schemaText(o.body_schema)) + '</pre></div>'
						+ '<div><b>' + esc(t('DOCS.D_RESP')) + '</b><pre class="kb-code">' + esc(schemaText(o.response_schema)) + '</pre></div>'
						+ '</div></td></tr>');
				}
			});
		});
		$('d-rows').innerHTML = html.join('');
	}

	function renderMeta() {
		var base = cfg.mqtt.base_topic, sn = data.serial || '<sn>';
		$('d-meta').innerHTML = '<span>' + esc(t('DOCS.META_FW')) + ' <b>' + esc(data.firmware || '–') + '</b></span>'
			+ '<span>' + esc(t('DOCS.META_API')) + ' ' + esc(data.api_version || '?') + ' · <b>' + esc(tf('DOCS.META_COUNT', data.ops.filter(function (o) { return o.status !== 'missing'; }).length)) + '</b></span>'
			+ '<span class="kb-sub-inline">' + esc(tf('DOCS.META_READ', fmtDateTime(data.fetched_at))) + '</span>';
		$('d-udp-topic').textContent = base + '/' + sn + '/udp/set';
		$('d-display-topic').textContent = base + '/' + sn + '/display/set';
	}

	function load(id) {
		boxId = id;
		open = {};
		$('d-rows').innerHTML = '<tr><td colspan="5" class="kb-muted">' + esc(t('COMMON.LOADING')) + '</td></tr>';
		ajax('getoperations', { id: id }).then(function (r) {
			if (!r.ok) { showError(r.error); $('d-rows').innerHTML = ''; return; }
			showError('');
			data = r;
			renderMeta();
			renderChips();
			renderRows();
		}).catch(function (e) { showError(e.message); });
	}

	$('d-chips').onclick = function (ev) {
		var btn = ev.target.closest('button[data-filter]');
		if (!btn || !data) return;
		filter = btn.getAttribute('data-filter');
		renderChips();
		renderRows();
	};
	var searchTimer = null;
	$('d-search').oninput = function () {
		var v = this.value;
		clearTimeout(searchTimer);
		searchTimer = setTimeout(function () { query = v.trim().toLowerCase(); if (data) renderRows(); }, 150);
	};
	$('d-rows').onclick = function (ev) {
		var btn = ev.target.closest('button[data-open]');
		if (!btn) return;
		var op = btn.getAttribute('data-open');
		open[op] = !open[op];
		renderRows();
	};
	$('d-rows').onchange = function (ev) {
		var box = ev.target.closest('input[data-op]');
		if (!box) return;
		var op = box.getAttribute('data-op'), on = box.checked;
		// Alle Haken sperren, bis die Config geschrieben ist (sonst parallele Schreibzugriffe)
		var all = Array.prototype.slice.call($('d-rows').querySelectorAll('input[data-op]'));
		all.forEach(function (i) { i.disabled = true; });
		ajax('setop', { id: boxId, op: op, enabled: on ? 1 : 0 }).then(function (r) {
			if (!r.ok) { box.checked = !on; showError(r.error); return; }
			showError('');
			data.ops.forEach(function (o) { if (o.op === op) o.status = on ? 'enabled' : 'available'; });
			data.ops = data.ops.filter(function (o) { return !(o.op === op && !on && o.topic === ''); });
			renderChips();
			renderRows();
		}).catch(function (e) { box.checked = !on; showError(e.message); })
			.then(function () {
				Array.prototype.forEach.call($('d-rows').querySelectorAll('input[data-op]'), function (i) { i.disabled = false; });
			});
	};
	$('d-refresh').onclick = function () {
		var btn = this;
		btn.disabled = true;
		ajax('refreshopenapi', { id: boxId }).then(function (r) {
			if (!r.ok) showError(r.error);
			load(boxId);
		}).catch(function (e) { showError(e.message); }).then(function () { btn.disabled = false; });
	};

	ajax('getconfig').then(function (c) {
		if (!c.ok) throw new Error(c.error || t('COMMON.ERROR'));
		cfg = c;
		if (!cfg.boxes.length) {
			$('d-rows').innerHTML = '<tr><td colspan="5" class="kb-muted">' + esc(t('DOCS.NO_BOX')) + '</td></tr>';
			$('d-refresh').disabled = true;
			return;
		}
		var first = PARAMS.get('box') || cfg.boxes[0].id;
		boxSegment($('d-boxes'), cfg.boxes, first, load);
		load(first);
	}).catch(function (e) { showError(e.message); });
}

// ---- Logfiles -------------------------------------------------------------------------------
function initLogfiles() {
	$('dg-run').onclick = function () {
		var btn = this;
		btn.disabled = true;
		ajax('diag').then(function (r) {
			if (!r.ok) { showError(r.error); return; }
			showError('');
			$('dg-out').textContent = r.text;
			$('dg-out').hidden = false;
			$('dg-copy').hidden = false;
		}).catch(function (e) { showError(e.message); }).then(function () { btn.disabled = false; });
	};
	$('dg-copy').onclick = function () {
		var text = $('dg-out').textContent;
		var done = function () { toast($('dg-toast'), t('LOGS.DIAG_COPIED')); };
		if (navigator.clipboard && window.isSecureContext) {
			navigator.clipboard.writeText(text).then(done);
		} else {
			var ta = document.createElement('textarea');
			ta.value = text;
			document.body.appendChild(ta);
			ta.select();
			try { document.execCommand('copy'); done(); } catch (e) { /* Auswahl bleibt stehen */ }
			document.body.removeChild(ta);
		}
	};
}

var inits = { overview: initOverview, settings: initSettings, mqtt: initMqtt, docs: initDocs, logfiles: initLogfiles };
if (inits[FORM]) inits[FORM]();
})();
</script>
