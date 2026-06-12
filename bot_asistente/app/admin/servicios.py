"""/admin/servicios — editor de los archivos de prompts + playground.

Permite ver y editar los .md que componen el system prompt del bot, y probar
en vivo qué respondería Dairo a un mensaje dado, sin pasar por WhatsApp.

Archivos editables (en `data/prompts/`):
  - dtgp-servicios.md         ← catálogo de servicios DTGP
  - dairo-identidad.md        ← persona Dairo
  - dtgp-empresa.md           ← contexto de la empresa
  - dairo-booking-playbook.md ← playbook de calificación + agenda
"""

from __future__ import annotations

import html
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin._shell import ICON_SPRITE, SHELL_STYLES, THEME_TOGGLE_JS, sidebar_html
from app.claude.anthropic_client import get_anthropic_client
from app.claude.prompts import (
    SYSTEM_PROMPT_EQUIPO,
    bloque_empresa,
    bloque_identidad,
    bloque_playbook,
    bloque_servicios,
    cargar_archivo,
    construir_system_prompt,
    _bloque_identidad_archivo,
)
from app.config import get_settings
from app.db.session import get_session
from app.logging_setup import log

router = APIRouter(prefix="/admin/servicios", tags=["admin-servicios"])
settings = get_settings()


def _check_auth(request: Request) -> bool:
    return "admin_token" in request.session


# Archivos editables — orden mostrado en la UI
ARCHIVOS = [
    ("dtgp-servicios.md",         "Servicios DTGP",       "Catálogo de servicios — lo que ofrecemos y cómo lo describimos."),
    ("dairo-identidad.md",        "Identidad Dairo",      "Persona, tono y reglas cuando el bot habla en el canal de Dairo."),
    ("dtgp-empresa.md",           "Sobre la empresa",     "Contexto de DTGP (qué somos, a quién servimos)."),
    ("dairo-booking-playbook.md", "Playbook prospectos",  "Cómo calificar a un prospecto y cuándo agendar la auditoría."),
]


def _path_seguro(nombre: str) -> Path:
    """Valida que el archivo esté dentro de data/prompts/ (anti path-traversal)."""
    base = settings.prompts_path.resolve()
    candidato = (base / nombre).resolve()
    if not str(candidato).startswith(str(base)):
        raise HTTPException(400, "Ruta no permitida")
    if not nombre.endswith(".md"):
        raise HTTPException(400, "Solo .md")
    return candidato


def _invalidar_cache_prompts() -> None:
    """Invalida los lru_cache de prompts.py tras editar un archivo."""
    bloque_identidad.cache_clear()
    bloque_empresa.cache_clear()
    bloque_servicios.cache_clear()
    bloque_playbook.cache_clear()
    _bloque_identidad_archivo.cache_clear()


# ─── GET /admin/servicios ─────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def vista_servicios(
    request: Request,
    archivo: str = "dtgp-servicios.md",
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401, "No autenticado")
    if archivo not in {a[0] for a in ARCHIVOS}:
        archivo = "dtgp-servicios.md"

    contenido = cargar_archivo(archivo)
    nombre_label = next((b for a, b, _ in ARCHIVOS if a == archivo), archivo)
    desc = next((c for a, _, c in ARCHIVOS if a == archivo), "")

    tabs_html = []
    for a, label, _desc in ARCHIVOS:
        active = " active" if a == archivo else ""
        tabs_html.append(
            f'<a href="?archivo={a}" class="prompt-tab{active}">{html.escape(label)}</a>'
        )

    body = _SERVICIOS_TEMPLATE
    body = body.replace("__SHELL_STYLES__", SHELL_STYLES)
    body = body.replace("__ICON_SPRITE__", ICON_SPRITE)
    body = body.replace("__SIDEBAR__", sidebar_html(active="servicios"))
    body = body.replace("__THEME_JS__", THEME_TOGGLE_JS)
    body = body.replace("{{tabs}}", "".join(tabs_html))
    body = body.replace("{{archivo}}", html.escape(archivo))
    body = body.replace("{{label}}", html.escape(nombre_label))
    body = body.replace("{{desc}}", html.escape(desc))
    body = body.replace("{{contenido}}", html.escape(contenido or ""))
    body = body.replace("{{chars}}", str(len(contenido)))
    body = body.replace("__PERSONA__", html.escape(settings.identidad_principal_persona_file or "dairo-identidad.md"))
    return HTMLResponse(body)


# ─── POST /admin/servicios/save ───────────────────────────────────────────


@router.post("/save")
async def guardar(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Guarda el contenido de un archivo de prompts.

    Hace backup (.bak.<timestamp>) antes de sobrescribir. Invalida lru_cache.
    """
    if not _check_auth(request):
        raise HTTPException(401)
    form = await request.form()
    archivo = (form.get("archivo") or "").strip()
    contenido = form.get("contenido")
    if not isinstance(contenido, str):
        return {"ok": False, "error": "contenido inválido"}
    if archivo not in {a[0] for a in ARCHIVOS}:
        return {"ok": False, "error": "archivo no permitido"}

    path = _path_seguro(archivo)
    if path.exists():
        # Backup rotando — mantenemos últimos 5
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        bak = path.with_suffix(f".md.bak.{ts}")
        shutil.copy2(path, bak)
        # Limpieza: dejar solo 5 backups más recientes
        baks = sorted(path.parent.glob(f"{path.stem}.md.bak.*"))
        for old in baks[:-5]:
            try: old.unlink()
            except Exception: pass

    path.write_text(contenido, encoding="utf-8")
    _invalidar_cache_prompts()
    autor = request.session.get("admin_user", "admin")
    log.warning(
        "admin.servicios.guardado",
        archivo=archivo, chars=len(contenido), autor=autor,
    )
    return {"ok": True, "chars": len(contenido), "archivo": archivo}


# ─── POST /admin/servicios/probar — Playground ────────────────────────────


@router.post("/probar")
async def probar_bot(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Simula una respuesta del bot a un mensaje dado.

    NO ejecuta tools — solo genera la respuesta de texto. Usa la persona
    'dairo-identidad.md' por defecto (consistente con el canal principal).
    """
    if not _check_auth(request):
        raise HTTPException(401)
    form = await request.form()
    mensaje = (form.get("mensaje") or "").strip() if isinstance(form.get("mensaje"), str) else ""
    modo = (form.get("modo") or "prospecto").strip().lower() if isinstance(form.get("modo"), str) else "prospecto"
    if not mensaje:
        return {"ok": False, "error": "mensaje vacío"}

    persona = settings.identidad_principal_persona_file or "dairo-identidad.md"
    if modo == "equipo":
        # Modo operativo: solo el system prompt del equipo (sin tools — solo
        # preview de cómo respondería; las tools no se ejecutan en playground).
        system = [
            {"type": "text", "text": SYSTEM_PROMPT_EQUIPO, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "## SOBRE DT GROWTH PARTNERS\n\n" + bloque_empresa(), "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "## SERVICIOS DE DTGP\n\n" + bloque_servicios(), "cache_control": {"type": "ephemeral"}},
        ]
    else:
        system = construir_system_prompt(persona_file=persona)

    try:
        client = get_anthropic_client()
        resp = await client.messages.create(
            model=settings.claude_model_principal,
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": mensaje}],
        )
    except Exception as e:
        log.exception("admin.servicios.probar_fail", error=str(e))
        return {"ok": False, "error": f"Error de Claude: {e}"}

    # Concatenar bloques de texto
    partes: list[str] = []
    for block in (resp.content or []):
        if getattr(block, "type", None) == "text":
            partes.append(getattr(block, "text", "") or "")
    salida = "\n".join(p for p in partes if p) or "(el bot respondió vacío)"

    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "input_tokens", 0) if usage else 0
    out_tok = getattr(usage, "output_tokens", 0) if usage else 0

    return {
        "ok": True,
        "respuesta": salida,
        "modelo": settings.claude_model_principal,
        "persona": persona if modo == "prospecto" else "SYSTEM_PROMPT_EQUIPO",
        "modo": modo,
        "tokens_in": in_tok,
        "tokens_out": out_tok,
    }


# ─── Template ─────────────────────────────────────────────────────────────


_SERVICIOS_TEMPLATE = """<!doctype html>
<html lang="es" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Servicios — Dairo</title>
__SHELL_STYLES__
<style>
  :root {
    --c-purple: #6366f1;
    --c-purple-hover: #4f46e5;
    --c-purple-soft: #ede9fe;
    --c-purple-softer: #f5f3ff;
    --c-text: #0f172a;
    --c-text-2: #475569;
    --c-text-3: #94a3b8;
    --c-border: #e5e7eb;
    --c-border-soft: #f1f5f9;
    --c-card: #ffffff;
    --c-success: #10b981;
    --c-danger: #ef4444;
  }
  [data-theme="dark"] {
    --c-text: #e2e8f0; --c-text-2: #94a3b8; --c-text-3: #64748b;
    --c-border: #1e293b; --c-border-soft: #1e293b; --c-card: #0f172a;
    --c-purple-soft: #312e81; --c-purple-softer: #1e1b4b;
  }
  .main { padding: 24px 28px; }
  .page-title { font-size: 22px; font-weight: 700; margin: 0 0 4px; color: var(--c-text); letter-spacing: -0.01em; }
  .page-subtitle { color: var(--c-text-2); font-size: 13px; margin-bottom: 18px; }

  .servicios-shell {
    display: grid; grid-template-columns: minmax(0, 1fr) 380px;
    gap: 18px; align-items: start;
  }
  @media (max-width: 1100px) { .servicios-shell { grid-template-columns: 1fr; } }

  .editor-card, .preview-card {
    background: var(--c-card); border: 1px solid var(--c-border);
    border-radius: 14px; padding: 18px;
    box-shadow: 0 1px 2px rgba(15,23,42,.04);
  }

  .prompt-tabs { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 14px; border-bottom: 1px solid var(--c-border); padding-bottom: 8px; }
  .prompt-tab {
    padding: 7px 14px; border-radius: 8px 8px 0 0;
    font-size: 13px; font-weight: 500;
    color: var(--c-text-2); text-decoration: none;
    border: 1px solid transparent;
  }
  .prompt-tab:hover { background: var(--c-purple-softer); color: var(--c-purple); }
  .prompt-tab.active {
    background: var(--c-purple-softer); color: var(--c-purple);
    border-color: var(--c-border); border-bottom-color: var(--c-purple-softer);
    margin-bottom: -1px;
  }

  .editor-head { display:flex; align-items: baseline; justify-content: space-between; margin-bottom: 8px; flex-wrap: wrap; gap: 8px; }
  .editor-head h2 { font-size: 16px; font-weight: 700; color: var(--c-text); margin: 0; }
  .editor-head .stats { font-size: 12px; color: var(--c-text-3); }
  .editor-desc { font-size: 12px; color: var(--c-text-2); margin-bottom: 12px; }

  #editor {
    width: 100%; min-height: 480px; max-height: 70vh;
    border: 1px solid var(--c-border); border-radius: 10px;
    padding: 14px; font: 13px ui-monospace, SFMono-Regular, Menlo, monospace;
    background: var(--c-card); color: var(--c-text);
    box-sizing: border-box; resize: vertical; outline: none;
    line-height: 1.6;
  }
  #editor:focus { border-color: var(--c-purple); box-shadow: 0 0 0 3px color-mix(in srgb, var(--c-purple) 15%, transparent); }

  .editor-actions { display: flex; gap: 10px; margin-top: 12px; align-items: center; }
  .btn-save {
    background: var(--c-purple); color: #fff; border: none;
    padding: 10px 18px; border-radius: 10px;
    font-size: 14px; font-weight: 600; cursor: pointer;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .btn-save:hover { background: var(--c-purple-hover); }
  .btn-save:disabled { opacity: .6; cursor: not-allowed; }
  .save-state { font-size: 12px; color: var(--c-text-3); }
  .save-state.ok { color: var(--c-success); }
  .save-state.err { color: var(--c-danger); }

  /* Playground */
  .preview-card h3 {
    font-size: 14px; font-weight: 700; color: var(--c-text); margin: 0 0 4px;
    display: flex; align-items: center; gap: 8px;
  }
  .preview-card .pg-desc { font-size: 12px; color: var(--c-text-2); margin-bottom: 12px; }
  .modo-tabs {
    display: flex; gap: 4px; margin-bottom: 10px;
    background: var(--c-purple-softer); padding: 3px; border-radius: 10px;
  }
  .modo-tab {
    flex: 1; padding: 6px 10px; border-radius: 8px;
    background: transparent; border: none; cursor: pointer;
    font-size: 12px; font-weight: 600; color: var(--c-text-2);
    transition: all .12s;
  }
  .modo-tab.active {
    background: var(--c-card); color: var(--c-purple);
    box-shadow: 0 1px 3px rgba(15,23,42,.08);
  }
  [data-theme="dark"] .modo-tab.active { background: var(--c-card); color: var(--c-purple); }
  .preview-card textarea {
    width: 100%; min-height: 80px; max-height: 200px; resize: vertical;
    border: 1px solid var(--c-border); border-radius: 10px;
    padding: 10px 12px; font: inherit; font-size: 13px;
    background: var(--c-card); color: var(--c-text);
    box-sizing: border-box; outline: none;
  }
  .preview-card textarea:focus { border-color: var(--c-purple); box-shadow: 0 0 0 3px color-mix(in srgb, var(--c-purple) 15%, transparent); }
  .btn-probar {
    background: linear-gradient(135deg, #f59e0b, #d97706); color: #fff;
    border: none; padding: 9px 14px; border-radius: 10px;
    font-size: 13px; font-weight: 600; cursor: pointer;
    margin-top: 8px; width: 100%;
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  }
  .btn-probar:hover { filter: brightness(1.05); }
  .btn-probar:disabled { opacity: .6; cursor: not-allowed; }
  .pg-respuesta {
    margin-top: 14px; padding: 12px 14px;
    background: linear-gradient(135deg, #ede9fe, #e0e7ff);
    border: 1px solid color-mix(in srgb, var(--c-purple) 20%, transparent);
    border-radius: 12px; font-size: 13px; line-height: 1.5;
    color: #1e1b4b; white-space: pre-wrap;
    display: none;
  }
  [data-theme="dark"] .pg-respuesta { background: linear-gradient(135deg, #312e81, #1e1b4b); color: #e0e7ff; }
  .pg-respuesta.show { display: block; }
  .pg-meta { font-size: 11px; color: var(--c-text-3); margin-top: 8px; }
  .pg-respuesta.error { background: #fef2f2; color: #991b1b; border-color: #fecaca; }
  [data-theme="dark"] .pg-respuesta.error { background: #7f1d1d; color: #fecaca; border-color: #b91c1c; }
  .pg-prompt-hint { font-size: 11px; color: var(--c-text-3); margin-top: 6px; }

  .toast-stack { position: fixed; bottom: 16px; right: 16px; display: flex; flex-direction: column; gap: 8px; z-index: 9999; }
  .toast { padding: 10px 16px; border-radius: 10px; font-size: 13px; color: #fff; background: var(--c-success); box-shadow: 0 4px 12px rgba(0,0,0,.15); }
  .toast.error { background: var(--c-danger); }
</style>
</head><body>
__ICON_SPRITE__
<div class="app">
  __SIDEBAR__
  <main class="main">
    <h1 class="page-title">Servicios y prompts del bot</h1>
    <p class="page-subtitle">Edita lo que Dairo dice sobre la empresa, sus servicios y su forma de atender. Los cambios se aplican al instante (sin reiniciar).</p>

    <div class="prompt-tabs">{{tabs}}</div>

    <div class="servicios-shell">
      <section class="editor-card">
        <div class="editor-head">
          <h2>{{label}}</h2>
          <span class="stats" id="char-stats">{{chars}} caracteres</span>
        </div>
        <p class="editor-desc">{{desc}} · Archivo: <code>{{archivo}}</code></p>
        <textarea id="editor" spellcheck="false" data-archivo="{{archivo}}">{{contenido}}</textarea>
        <div class="editor-actions">
          <button type="button" class="btn-save" id="btn-save">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
            Guardar cambios
          </button>
          <span class="save-state" id="save-state"></span>
        </div>
      </section>

      <aside class="preview-card">
        <h3>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
          Probar respuesta del bot
        </h3>
        <p class="pg-desc">Escribe un mensaje hipotético y mira qué respondería Dairo con los prompts actuales (sin enviar a WhatsApp, sin ejecutar tools).</p>
        <div class="modo-tabs">
          <button type="button" class="modo-tab active" data-modo="prospecto" title="Cómo respondería a un lead que llega por pauta">Prospecto (lead)</button>
          <button type="button" class="modo-tab" data-modo="equipo" title="Cómo respondería a un miembro del equipo o cliente activo">Equipo / cliente</button>
        </div>
        <textarea id="pg-input" placeholder="Ej: ¿Cuánto cuesta el plan starter? · Tengo un negocio de ropa y necesito anuncios"></textarea>
        <button type="button" class="btn-probar" id="btn-probar">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>
          Probar respuesta
        </button>
        <div class="pg-prompt-hint">Usa la persona configurada: <code>__PERSONA__</code></div>
        <div id="pg-respuesta" class="pg-respuesta"></div>
      </aside>
    </div>
  </main>
</div>
<div class="toast-stack" id="toast-stack"></div>
__THEME_JS__
<script>
(function(){
  var editor = document.getElementById('editor');
  var stats = document.getElementById('char-stats');
  var btnSave = document.getElementById('btn-save');
  var saveState = document.getElementById('save-state');
  var stack = document.getElementById('toast-stack');

  function toast(msg, err){
    var el = document.createElement('div');
    el.className = 'toast' + (err ? ' error' : '');
    el.textContent = msg; stack.appendChild(el);
    setTimeout(function(){ el.style.opacity='0'; el.style.transform='translateY(20px)'; }, 2400);
    setTimeout(function(){ stack.removeChild(el); }, 2700);
  }

  editor.addEventListener('input', function(){
    stats.textContent = editor.value.length + ' caracteres';
    saveState.textContent = 'Cambios sin guardar';
    saveState.className = 'save-state';
  });
  // Ctrl+S
  editor.addEventListener('keydown', function(e){
    if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); guardar(); }
  });

  async function guardar(){
    btnSave.disabled = true;
    saveState.textContent = 'Guardando…';
    saveState.className = 'save-state';
    try {
      var fd = new FormData();
      fd.append('archivo', editor.dataset.archivo);
      fd.append('contenido', editor.value);
      var r = await fetch('/admin/servicios/save', {
        method: 'POST', body: fd,
        headers: {'Accept':'application/json','X-Requested-With':'fetch'},
      });
      var data = await r.json().catch(function(){ return {ok:false}; });
      if (r.ok && data.ok) {
        saveState.textContent = '✓ Guardado · ' + data.chars + ' caracteres · cache invalidado';
        saveState.className = 'save-state ok';
        toast('Guardado');
      } else {
        saveState.textContent = 'Error: ' + (data.error || r.status);
        saveState.className = 'save-state err';
        toast('Error al guardar', true);
      }
    } catch(err) {
      saveState.textContent = 'Error de red';
      saveState.className = 'save-state err';
      toast('Error de red', true);
    }
    btnSave.disabled = false;
  }
  btnSave.addEventListener('click', guardar);

  // ── Playground ───────────────────────────────────────────────────
  var pgInput = document.getElementById('pg-input');
  var pgBtn = document.getElementById('btn-probar');
  var pgResp = document.getElementById('pg-respuesta');
  var modoTabs = document.querySelectorAll('.modo-tab');
  var modoActual = 'prospecto';
  modoTabs.forEach(function(t){
    t.addEventListener('click', function(){
      modoActual = t.dataset.modo;
      modoTabs.forEach(function(x){ x.classList.toggle('active', x === t); });
      pgInput.placeholder = modoActual === 'equipo'
        ? 'Ej: regístrale la cuenta de cobro a Anita · cómo va Equilibrio este mes · pásame el reporte'
        : 'Ej: ¿Cuánto cuesta el plan starter? · Tengo un negocio de ropa y necesito anuncios';
    });
  });

  async function probar(){
    var texto = pgInput.value.trim();
    if (!texto) return;
    pgBtn.disabled = true;
    pgBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" opacity=".3"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"><animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur=".9s" repeatCount="indefinite"/></path></svg> Pensando…';
    pgResp.className = 'pg-respuesta show';
    pgResp.textContent = 'Generando respuesta… (puede tardar 5-15 segundos)';
    try {
      var fd = new FormData(); fd.append('mensaje', texto); fd.append('modo', modoActual);
      var r = await fetch('/admin/servicios/probar', {
        method: 'POST', body: fd,
        headers: {'Accept':'application/json','X-Requested-With':'fetch'},
      });
      var data = await r.json().catch(function(){ return {ok:false}; });
      if (r.ok && data.ok) {
        pgResp.className = 'pg-respuesta show';
        pgResp.innerHTML = '';
        var txt = document.createElement('div');
        txt.textContent = data.respuesta;
        var meta = document.createElement('div');
        meta.className = 'pg-meta';
        meta.textContent = (data.modo || modoActual) + ' · ' + data.modelo + ' · ' + data.tokens_in + ' in / ' + data.tokens_out + ' out';
        pgResp.appendChild(txt); pgResp.appendChild(meta);
      } else {
        pgResp.className = 'pg-respuesta show error';
        pgResp.textContent = 'Error: ' + (data.error || r.status);
      }
    } catch(err) {
      pgResp.className = 'pg-respuesta show error';
      pgResp.textContent = 'Error de red: ' + err.message;
    }
    pgBtn.disabled = false;
    pgBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg> Probar respuesta';
  }
  pgBtn.addEventListener('click', probar);
  pgInput.addEventListener('keydown', function(e){
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); probar(); }
  });
})();
</script>
</body></html>"""
