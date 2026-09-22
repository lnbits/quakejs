window.QuakeTouchControls = class {
  constructor({root, canvas, overlay, enabled, command, look, menu}) {
    this.root = root
    this.enabled = enabled
    this.command = command
    this.look = look
    this.held = new Set()
    this.pointers = new Map()
    const stick = root.querySelector('[data-touch="move"]')
    const aim = root.querySelector('[data-touch="look"]')
    const knob = stick.querySelector('span')
    this.knob = knob
    this.stick = stick
    this.aim = aim
    const updateMove = event => {
      const box = stick.getBoundingClientRect()
      const radius = box.width / 2
      let x = (event.clientX - box.left - radius) / radius
      let y = (event.clientY - box.top - radius) / radius
      const length = Math.max(1, Math.hypot(x, y))
      x /= length; y /= length
      knob.style.transform = `translate(${x * radius * .55}px, ${y * radius * .55}px)`
      this.hold('forward', y < -.22)
      this.hold('back', y > .22)
      this.hold('moveleft', x < -.22)
      this.hold('moveright', x > .22)
    }
    for (const target of root.querySelectorAll('[data-touch]')) {
      target.addEventListener('pointerdown', event => {
        if (!enabled() || (event.pointerType === 'mouse' && event.button !== 0)) return
        event.preventDefault()
        const action = target.dataset.touch
        if ([...this.pointers.values()].some(pointer => pointer.action === action)) return
        target.setPointerCapture(event.pointerId)
        this.pointers.set(event.pointerId, {target, action, x:event.clientX, y:event.clientY})
        target.classList.add('pressed')
        if (action === 'move') updateMove(event)
        else if (action === 'weapon') command('weapnext')
        else if (action === 'menu') menu()
        else if (action !== 'look') this.hold(action, true)
        // Focus without scrolling; canvas keyboard/mouse input stays separate.
        if (action !== 'menu') canvas.focus({preventScroll:true})
      })
      target.addEventListener('pointermove', event => {
        const pointer = this.pointers.get(event.pointerId)
        if (!pointer) return
        event.preventDefault()
        if (!enabled()) { this.release(); return }
        if (pointer.action === 'move') updateMove(event)
        if (pointer.action === 'look') {
          const clamp = value => Math.max(-100, Math.min(100, value))
          look(Math.round(clamp(event.clientX - pointer.x) * 2.5), Math.round(clamp(event.clientY - pointer.y) * 2.5))
          pointer.x = event.clientX; pointer.y = event.clientY
        }
      })
      for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) target.addEventListener(name, event => this.end(event.pointerId))
      target.addEventListener('contextmenu', event => event.preventDefault())
    }
    this.onBlur = () => this.release()
    this.onVisibility = () => { if (document.hidden) menu(); this.update() }
    window.addEventListener('blur', this.onBlur)
    window.addEventListener('pagehide', this.onBlur)
    window.addEventListener('orientationchange', this.onBlur)
    document.addEventListener('visibilitychange', this.onVisibility)
    this.observer = new MutationObserver(() => this.update())
    this.observer.observe(overlay, {attributes:true, attributeFilter:['hidden']})
    this.update()
  }
  hold(action, down) {
    if (this.held.has(action) === down) return
    if (down) this.held.add(action)
    else this.held.delete(action)
    this.command((down ? '+' : '-') + action)
  }
  end(id) {
    const pointer = this.pointers.get(id)
    if (!pointer) return
    this.pointers.delete(id)
    pointer.target.classList.remove('pressed')
    if (pointer.target.hasPointerCapture(id)) pointer.target.releasePointerCapture(id)
    if (pointer.action === 'move') {
      for (const action of ['forward','back','moveleft','moveright']) this.hold(action, false)
      this.knob.style.transform = ''
    } else if (this.held.has(pointer.action)) this.hold(pointer.action, false)
  }
  release() {
    for (const id of [...this.pointers.keys()]) this.end(id)
    for (const action of [...this.held]) this.hold(action, false)
  }
  update() {
    this.root.hidden = !this.enabled()
    if (this.root.hidden) this.release()
  }
}
