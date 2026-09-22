// Binary game packets go straight to the dedicated server, without base64 relays.
window.QuakeTransport = class {
  constructor(id, token, onState, onError) {
    this.id=id; this.token=token; this.onState=onState; this.onError=onError
    this.rtt=null; this.jitter=0; this.dropped=0; this.lastReceived=performance.now()
    this.pending=new Map(); this.counter=0; this.closed=false
    this.connect()
    this.timer=setInterval(() => {
      if (this.socket?.readyState !== WebSocket.OPEN) return
      const id=++this.counter
      this.pending.set(id,performance.now())
      this.sendJSON({type:'ping',id})
      for(const [probe,at] of this.pending) if(performance.now()-at>10000) this.pending.delete(probe)
    },2000)
  }
  connect() {
    if(this.closed) return
    const ws=this.socket=new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/quakejs/api/v1/ws/${this.id}`)
    ws.binaryType='arraybuffer'
    ws.onopen=() => { this.sendJSON({token:this.token}); this.bind(this.module) }
    ws.onmessage=event => {
      this.lastReceived=performance.now()
      if(event.data instanceof ArrayBuffer) {
        if(this.module) {
          const queue=this.module._webrtc.recvQueue
          if(queue.length<128) queue.push({peerId:0,data:new Uint8Array(event.data)})
          else this.dropped++
        }
        return
      }
      const data=JSON.parse(event.data)
      if(data.type==='state') this.onState(data.data)
      else if(data.type==='error') this.onError(data.message)
      else if(data.type==='pong' && this.pending.has(data.id)) {
        const rtt=performance.now()-this.pending.get(data.id)
        this.jitter=this.rtt===null?0:this.jitter*.8+Math.abs(rtt-this.rtt)*.2
        this.rtt=rtt;this.pending.delete(data.id)
      }
    }
    ws.onclose=() => {
      if(this.module) delete this.module._webrtc.peers[0]
      if(!this.closed) { this.onError('Connection lost. Reconnecting…'); this.reconnect=setTimeout(()=>this.connect(),2000+Math.random()*1000) }
    }
    ws.onerror=()=>ws.close()
  }
  bind(module) {
    this.module=module
    if(module && this.socket?.readyState===WebSocket.OPEN) module._webrtc.peers[0]={dc:{readyState:'open',send:bytes=>{
      if(this.socket.readyState!==WebSocket.OPEN || this.socket.bufferedAmount>32768) { this.dropped++;return }
      this.socket.send(bytes)
    }}}
  }
  sendJSON(data) {
    if(this.socket?.readyState!==WebSocket.OPEN) throw new Error('Connecting to the server. Please retry shortly.')
    this.socket.send(JSON.stringify(data))
  }
  stats() { return {rtt:this.rtt,jitter:this.jitter,dropped:this.dropped,queueMs:(this.socket?.bufferedAmount||0)/25,silentMs:performance.now()-this.lastReceived} }
  close() { this.closed=true;clearInterval(this.timer);clearTimeout(this.reconnect);this.socket?.close() }
}
