window.QuakeAssets={
  manifest:{maps:["aggressor", "oa_dm7", "oa_minia", "czest1dm", "oa_shine", "kaos2"],size:198429979,sha256:'f5e9f8e22f915c83ccbcf0732a79d367876e5ae96848e8ab4fd347d4db01cf1d'},
  async load(progress) {
    let failure
    for(const cache of ['force-cache','reload']) {
      try { return await this.download(progress,cache) }
      catch(error) { failure=error;progress(0) }
    }
    throw failure
  },
  async download(progress,cache) {
    const response=await fetch('/quakejs/static/arena/baseoa/arena.pk3?v='+this.manifest.sha256,{cache})
    if(!response.ok) throw new Error('Game assets could not be downloaded. Reload to retry.')
    const data=new Uint8Array(this.manifest.size), reader=response.body.getReader()
    let offset=0
    while(true) {
      const {done,value}=await reader.read()
      if(done) break
      if(offset+value.length>data.length) {
        await reader.cancel()
        throw new Error('Invalid game asset size.')
      }
      data.set(value,offset);offset+=value.length;progress(offset/data.length)
    }
    if(offset!==data.length) {
      if(new TextDecoder().decode(data.subarray(0,Math.min(offset,100))).startsWith('version https://git-lfs.github.com/spec/v1')) {
        throw new Error('Game files are missing from this server. Contact the arena owner.')
      }
      throw new Error('Game asset download was incomplete. Reload to retry.')
    }
    if(crypto.subtle) {
      const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',data))].map(v=>v.toString(16).padStart(2,'0')).join('')
      if(hash!==this.manifest.sha256) throw new Error('Game assets failed their integrity check.')
    }
    return data
  }
}
