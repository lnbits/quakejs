window.QuakeAssets={
  manifest:{maps:['aggressor','oa_dm1','oa_dm2','kaos2'],size:194745636,sha256:'560441cfa784b564531708d1dbefc1a2616b43af161da33b6f4a5e5fc3000642'},
  async load(progress) {
    const response=await fetch('/quakejs/static/arena/baseoa/arena.pk3?v='+this.manifest.sha256,{cache:'force-cache'})
    if(!response.ok) throw new Error('Game assets could not be downloaded. Reload to retry.')
    const data=new Uint8Array(this.manifest.size), reader=response.body.getReader()
    let offset=0
    while(true) {
      const {done,value}=await reader.read()
      if(done) break
      if(offset+value.length>data.length) throw new Error('Invalid game asset size.')
      data.set(value,offset);offset+=value.length;progress(offset/data.length)
    }
    if(offset!==data.length) throw new Error('Game asset download was incomplete. Reload to retry.')
    if(crypto.subtle) {
      const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',data))].map(v=>v.toString(16).padStart(2,'0')).join('')
      if(hash!==this.manifest.sha256) throw new Error('Game assets failed their integrity check.')
    }
    return data
  }
}
