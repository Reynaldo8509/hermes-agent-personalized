"""Bridge bajo demanda de Hermes/MAX a la PC de display_one por Tailscale."""
from __future__ import annotations
import base64, json, re, subprocess
from pathlib import Path
from typing import Any
import yaml
from tools.registry import tool_error, tool_result

CFG = Path('$HOME/hermes/config/display_one-node.yaml')

def config():
    try: data = yaml.safe_load(CFG.read_text())
    except (OSError, yaml.YAMLError) as exc: raise RuntimeError('config display_one-PC no disponible') from exc
    req = ('transport','endpoint','ssh_user','ssh_key','known_hosts','runner')
    if not isinstance(data, dict) or data.get('node_id') != 'display_one-PC': raise RuntimeError('identidad display_one-PC invalida')
    if data.get('controller') != 'MAX' or data.get('peer_nodes') != []: raise RuntimeError('display_one-PC solo admite MAX y no tiene pares')
    if data.get('transport') != 'tailscale' or any(not data.get(x) for x in req[1:]): raise RuntimeError('config Tailscale incompleta')
    return data

def ssh():
    c=config()
    return ['ssh','-i',str(c['ssh_key']),'-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o',f"UserKnownHostsFile={c['known_hosts']}",'-o','ConnectTimeout=10',f"{c['ssh_user']}@{c['endpoint']}"]

def run(args):
    c=config(); payload=base64.b64encode(json.dumps(args,separators=(',',':')).encode()).decode('ascii')
    remote=f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{c['runner']}\" -Payload {payload}"
    p=subprocess.run(ssh()+[remote],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180,check=False)
    if not p.stdout.strip(): raise RuntimeError('runner display_one-PC sin respuesta')
    try: data=json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError,TypeError) as exc: raise RuntimeError('respuesta invalida del runner display_one-PC') from exc
    return {'success':p.returncode==0 and bool(data.get('ok')),'node_id':c['node_id'],**data}

STATUS={'name':'display_one_node_status','description':'Estado de display_one-PC por Tailscale sin agente persistente.','parameters':{'type':'object','properties':{},'additionalProperties':False}}
TASK={'name':'display_one_node_task','description':'Ejecuta tareas bajo demanda en display_one-PC. stacher-download usa el motor de Stacher para YouTube autorizado a 720p maximo con audio en Downloads.','parameters':{'type':'object','properties':{'operation':{'type':'string','enum':['exec','interactive-exec','cua-call','stacher-launch','stacher-status','stacher-download','network-status','cua-status','read_file','write_file']},'command':{'type':'string','maxLength':20000},'tool':{'type':'string','maxLength':128},'arguments':{'type':'object'},'url':{'type':'string','maxLength':4096},'path':{'type':'string','maxLength':1024},'content':{'type':'string','maxLength':1000000}},'required':['operation'],'additionalProperties':False}}

def status(_,**__):
    try:
        c=config(); p=subprocess.run(ssh()+['cmd.exe /c echo %COMPUTERNAME%'],capture_output=True,text=True,timeout=15,check=False)
        return tool_result({'success':p.returncode==0,'node_id':c['node_id'],'transport':c['transport'],'tailscale_ip':c['endpoint'],'ssh_reachable':p.returncode==0,'hostname':p.stdout.strip(),'on_demand':True,'persistent_agent':False,'tailscale_unattended':True,'advertise_exit_node':False})
    except Exception as exc: return tool_error(f'display_one-PC no disponible: {type(exc).__name__}')

def task(a,**__):
    op=str(a.get('operation') or '')
    if op not in {'exec','interactive-exec','cua-call','stacher-launch','stacher-status','stacher-download','network-status','cua-status','read_file','write_file'}: return tool_error('operacion no permitida')
    requested_op=op
    if op=='cua-call':
        tool=str(a.get('tool') or '')
        if not tool or not tool.replace('_','').isalnum(): return tool_error('cua-call tool invalido')
        payload=base64.b64encode(json.dumps(a.get('arguments') or {},separators=(',',':')).encode()).decode('ascii')
        command=("$c='C:\\ProgramData\\Hermes\\cua-driver\\cua-driver.exe'; "
                 "$b=[Convert]::FromBase64String('"+payload+"'); "
                 "[Text.Encoding]::UTF8.GetString($b) | & $c call "+tool+" --socket '\\\\.\\pipe\\cua-driver'")
        a={**a,'operation':'interactive-exec','command':command}
        op='interactive-exec'
    if op=='stacher-download':
        url=str(a.get('url') or '')
        if not re.match(r'^https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+$',url,re.I): return tool_error('stacher-download requiere un enlace directo de YouTube')
        payload=base64.b64encode(url.encode()).decode('ascii')
        command=("$b=[Convert]::FromBase64String('"+payload+"'); $u=[Text.Encoding]::UTF8.GetString($b); "
                 "$app='C:\\Users\\liaya\\AppData\\Local\\Stacher7\\Stacher7.exe'; "
                 "$yt='C:\\Users\\liaya\\.stacher\\yt-dlp.exe'; $ff='C:\\Users\\liaya\\.stacher\\ffmpeg.exe'; "
                 "$dest='C:\\Users\\liaya\\Downloads'; if(-not (Test-Path $yt)){throw 'yt-dlp de Stacher no encontrado'}; "
                 "if(-not (Test-Path $dest)){New-Item -ItemType Directory -Path $dest -Force | Out-Null}; "
                 "if(-not (Get-Process -Name Stacher7 -ErrorAction SilentlyContinue)){Start-Process -FilePath $app -WindowStyle Minimized}; "
                 "$args=@($u,'--no-playlist','-f','bestvideo[height<=720]+bestaudio/best[height<=720]','--merge-output-format','mp4','--restrict-filenames','-o',($dest+'\\%(title)s.%(ext)s')); "
                 "if(Test-Path $ff){$args=@($u,'--no-playlist','-f','bestvideo[height<=720]+bestaudio/best[height<=720]','--merge-output-format','mp4','--ffmpeg-location',$ff,'--restrict-filenames','-o',($dest+'\\%(title)s.%(ext)s'))}; "
                 "$p=Start-Process -FilePath $yt -ArgumentList $args -WorkingDirectory $dest -WindowStyle Hidden -PassThru; "
                 "[pscustomobject]@{queued=$true;pid=$p.Id;path=$dest;quality='720p-max+audio';engine=$yt} | ConvertTo-Json -Compress")
        a={**a,'operation':'interactive-exec','command':command}
        op='interactive-exec'
    if op=='stacher-launch':
        a={**a,'operation':'interactive-exec','command':'$p=\"C:\\Users\\liaya\\AppData\\Local\\Stacher7\\Stacher7.exe\"; $x=Get-Process -Name Stacher7 -ErrorAction SilentlyContinue | Select-Object -First 1; if (-not $x) { $x=Start-Process -FilePath $p -PassThru }; [pscustomobject]@{path=$p; pid=$x.Id; running=$true} | ConvertTo-Json -Compress'}
        op='interactive-exec'
    elif op=='stacher-status':
        a={**a,'operation':'interactive-exec','command':'$p=\"C:\\Users\\liaya\\AppData\\Local\\Stacher7\\Stacher7.exe\"; $x=Get-Process -Name Stacher7 -ErrorAction SilentlyContinue | Select-Object -First 1; $processId=$null; if($x){$processId=$x.Id}; [pscustomobject]@{path=$p; installed=(Test-Path $p); running=($null -ne $x); pid=$processId} | ConvertTo-Json -Compress'}
        op='interactive-exec'
    if op in {'exec','interactive-exec'} and not isinstance(a.get('command'),str): return tool_error(f'{op} requiere command')
    if op in {'read_file','write_file'} and not isinstance(a.get('path'),str): return tool_error(f'{op} requiere path')
    try: return tool_result({'transport':'tailscale','on_demand':True,'requested_operation':requested_op,**run({k:v for k,v in a.items() if k in {'operation','command','tool','arguments','url','path','content'}})})
    except Exception as exc: return tool_error(f'display_one-PC no ejecutada: {str(exc) or type(exc).__name__}')

def register(ctx:Any):
    ctx.register_tool(name='display_one_node_status',toolset='hermy-hq',schema=STATUS,handler=status,emoji='🖥️')
    ctx.register_tool(name='display_one_node_task',toolset='hermy-hq',schema=TASK,handler=task,emoji='🖥️')
    def download(raw):
        url=(raw or '').strip().split()[0] if (raw or '').strip() else ''
        return str(task({'operation':'stacher-download','url':url}))
    ctx.register_command('display_one_pc_descargar_video',download,'Descarga YouTube autorizado en display_one-PC a 720p con audio','<URL>')
    ctx.register_command('display_one-pc-descargar-video',download,'Descarga YouTube autorizado en display_one-PC a 720p con audio','<URL>')
