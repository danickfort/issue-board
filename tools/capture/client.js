await Promise.all([document.fonts.load('15px CaptureMono'),document.fonts.load('15px CaptureSymbols')]);
const term = new Terminal({cols:140,rows:38,fontFamily:'CaptureMono, CaptureSymbols, monospace',fontSize:15,lineHeight:1.12,cursorBlink:false,scrollback:2000,theme:{background:'#181926',foreground:'#cad3f5',cursor:'#8bd5ca',selectionBackground:'#494d64',black:'#181926',red:'#ed8796',green:'#a6da95',yellow:'#eed49f',blue:'#8aadf4',magenta:'#c6a0f6',cyan:'#8bd5ca',white:'#cad3f5'}});
const fit = new FitAddon.FitAddon(); term.loadAddon(fit); term.open(document.getElementById('terminal')); fit.fit();
try { term.loadAddon(new WebglAddon.WebglAddon()); } catch(error) { console.warn('WebGL unavailable; using DOM renderer', error); }
const ws = new WebSocket(`ws://${location.host}/terminal`);
const send = data => {if(ws.readyState !== WebSocket.OPEN) throw new Error('Terminal disconnected');ws.send(JSON.stringify({type:'input',data}));};
const resize = () => {fit.fit();if(ws.readyState === 1)ws.send(JSON.stringify({type:'resize',cols:term.cols,rows:term.rows}));};
ws.onopen = () => {resize();term.focus();};
ws.onmessage = event => term.write(event.data);
ws.onclose = () => {document.getElementById('chapter').textContent='Session closed · reload for a fresh demo';};
term.onData(send); window.addEventListener('resize',resize);
const sleep = ms => new Promise(resolve=>setTimeout(resolve,ms));
const screen = () => Array.from({length:term.rows},(_,i)=>term.buffer.active.getLine(term.buffer.active.viewportY+i)?.translateToString(true)||'').join('\n');
const chapter = (title,hint='') => {document.getElementById('chapter').textContent=title;document.getElementById('hint').textContent='  '+hint;};
const waitFor = async (text,timeout=15000) => {const deadline=Date.now()+timeout;while(!screen().includes(text)){if(Date.now()>deadline)throw new Error(`Screen did not show: ${text}`);await sleep(100);}await sleep(200);};
const type = async text => {send(text);await sleep(350);};
window.capture = {term,send,screen,chapter,waitFor,sleep,type,resize};
document.getElementById('tour').onclick = () => window.capture.walkthrough().catch(error=>{chapter('Capture stopped',error.message);console.error(error);});
// Drive the real app through its PTY. A fresh page starts a fresh in-memory demo.
let touring = false;
window.capture.walkthrough = async () => {
  if(touring) throw new Error('A walkthrough is already running');
  await waitFor('issue-board ›',5000);
  if(term.buffer.active.type !== 'normal' || !screen().includes('issue-board ›')) throw new Error('Reload the page to start a fresh shell before the walkthrough');
  touring = true;
  document.body.classList.add('recording');
  const step = async (id,title,hint,action,expect,hold=3000) => {
    window.capture.step = id; chapter(title,hint);
    if(action) await action();
    if(expect) await waitFor(expect,10000);
    await sleep(hold);
  };
  try {
    await step('launch','One command opens your project board','Offline YouTrack demo',async()=>{send('issueboard --demo youtrack');await sleep(900);send('\r');},'Keyboard navigation for the board',2000);
    send('R');await sleep(500);
    await step('board','Your work, across projects','← / → columns · ↑ / ↓ stories',null,'8 issues',3500);
    await step('move','Move a story to the next column','m → Enter',()=>send('m'),'Move DEMO-1',2200);
    await step('moved','Open → In Progress','The selected story follows its new column',()=>send('\r'),'State: In Progress',3500);
    await step('details','Open a story without leaving the board','↓ → Enter',async()=>{send('j');await sleep(900);send('\r');},'Comments',3500);
    await step('comments','Read the conversation','Esc → Shift+C · Markdown and restricted comments',async()=>{send('\u001b');await sleep(900);send('C');},'3 comments, newest first',4000);
    await step('write','Reply from the terminal','c → type a comment → Enter',()=>send('c'),'Comment on DEMO-3',700);
    send('Ready for review.');await sleep(1700);send('\r');
    await waitFor('Comment added to DEMO-3',10000);await sleep(1000);
    await step('replied','Comment saved','Shift+C reopens the updated conversation',()=>send('C'),'4 comments, newest first',3500);
    await step('merge-requests','Review the story’s merge requests','Esc → g · pipelines, approvals and discussion threads',async()=>{send('\u001b');await sleep(900);send('g');},'pipeline passed',4500);
    await step('draft','Inspect another merge request','↓ · draft status and failed pipeline',()=>send('\u001b[B'),'!2  draft · open',2500);
    await step('backlog','Keep the backlog close','Esc → Shift+B · search and page through issues',async()=>{send('\u001b');await sleep(900);send('B');},'Product backlog',4000);
    await step('finish','Back to the board','Esc · YouTrack, GitLab and Jira in one terminal app',()=>send('\u001b'),'8 issues',3500);
    window.capture.done = true;
  } catch(error) {
    window.capture.error = error.message;
    chapter('Capture stopped',error.message);
    throw error;
  } finally { touring=false; }
};
