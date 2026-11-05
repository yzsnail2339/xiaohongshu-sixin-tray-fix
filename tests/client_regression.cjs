// Tests execute locally supplied client code with Electron test doubles.
// Node's VM is not a security sandbox: use only the supported, trusted client.
// Run via: python tests/run_local_client_tests.py --asar <original-app.asar>
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { EventEmitter } = require('node:events')

const args = process.argv.slice(2)
if (args.length !== 4 || args[0] !== '--original-dir' || args[2] !== '--patched-dir') {
  console.error('Usage: node tests/client_regression.cjs --original-dir <dir> --patched-dir <dir>')
  process.exit(2)
}
const sourceDirectories = {
  original: path.resolve(args[1]),
  patched: path.resolve(args[3]),
}
for (const directory of Object.values(sourceDirectories)) {
  if (!fs.statSync(directory).isDirectory()) throw new Error('Source path must be a directory')
}

function boot(variant) {
  const created = []
  const ready = []
  const cache = new Map()
  let tray
  let quitting = false
  let intervalId = 0
  const intervals = new Set()
  const app = new EventEmitter()
  app.name = '小红书私信通'
  app.commandLine = { appendSwitch() {} }
  app.whenReady = () => ({ then: (callback) => ready.push(callback) })
  app.relaunch = () => { throw new Error('Unexpected app relaunch') }
  app.quit = () => {
    if (quitting) return
    quitting = true
    app.emit('before-quit')
    for (const win of created.filter((win) => !win.destroyed)) win.close()
    app.emit('quit')
  }
  app.exit = () => {
    quitting = true
    for (const win of created.filter((win) => !win.destroyed)) win.destroy()
    app.emit('quit')
  }
  const ipcMain = new EventEmitter()
  function contents() {
    const content = new EventEmitter()
    content.loadFile = () => Promise.resolve()
    content.send = () => {}
    return content
  }
  class BrowserWindow extends EventEmitter {
    constructor(options) {
      super()
      this.options = options
      this.id = created.length + 1
      this.destroyed = false
      this.visible = false
      this.focused = false
      this.minimized = false
      this.skipTaskbar = false
      this.webContents = contents()
      this.view = null
      this.calls = { show: 0, focus: 0, restore: 0, destroy: 0 }
      created.push(this)
    }
    alive() { if (this.destroyed) throw new Error('Object has been destroyed') }
    isDestroyed() { return this.destroyed }
    isFocused() { this.alive(); return this.focused }
    isMinimized() { this.alive(); return this.minimized }
    getTitle() { this.alive(); return `Test account ${this.id}` }
    getPosition() { this.alive(); return [0, 0] }
    getBounds() { this.alive(); return { x: 0, y: 0, width: 1280, height: 800 } }
    getBrowserView() { this.alive(); return this.view }
    setBrowserView(view) { this.alive(); this.view = view }
    setSkipTaskbar(value) { this.alive(); this.skipTaskbar = value }
    loadURL() { this.alive(); return Promise.resolve() }
    loadFile() { this.alive(); return Promise.resolve() }
    show() { this.alive(); this.visible = true; this.calls.show++; this.emit('show') }
    hide() { this.alive(); this.visible = false; this.focused = false; this.emit('hide') }
    focus() {
      this.alive()
      for (const win of created) win.focused = false
      this.focused = true
      this.calls.focus++
      this.emit('focus')
    }
    minimize() { this.alive(); this.minimized = true; this.emit('minimize') }
    restore() { this.alive(); this.minimized = false; this.calls.restore++; this.emit('restore') }
    close() {
      this.alive()
      const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true } }
      this.emit('close', event)
      if (!event.defaultPrevented) this.destroy()
      return event
    }
    destroy() {
      this.alive()
      this.calls.destroy++
      this.destroyed = true
      this.visible = false
      this.focused = false
      this.emit('closed')
      if (!created.some((win) => !win.destroyed)) app.emit('window-all-closed')
    }
    static getFocusedWindow() { return created.find((win) => !win.destroyed && win.focused) }
    static getAllWindows() { return created.filter((win) => !win.destroyed) }
  }
  class BrowserView {
    constructor() { this.webContents = contents() }
    setBounds() {}
    setAutoResize() {}
  }
  class Tray extends EventEmitter {
    setToolTip() {}
    setContextMenu(menu) { this.menu = menu }
    setImage(image) { this.image = image }
    destroy() { this.destroyed = true }
  }
  const electron = {
    app, BrowserWindow, BrowserView, ipcMain, Tray,
    Menu: { buildFromTemplate: (template) => template },
    nativeImage: { createFromPath: (imagePath) => imagePath },
    session: {}, dialog: {},
  }
  const context = vm.createContext({
    console, URL, process: { platform: 'win32' },
    setInterval: () => { const id = ++intervalId; intervals.add(id); return id },
    clearInterval: (id) => intervals.delete(id),
  })
  context.global = context
  function load(relative) {
    const selected = variant === 'patched' && ['main/windows.js', 'main/tray/tray.js'].includes(relative)
      ? 'patched' : 'original'
    const filename = path.join(sourceDirectories[selected], relative)
    if (cache.has(filename)) return cache.get(filename).exports
    const module = { exports: {} }
    cache.set(filename, module)
    function moduleRequire(name) {
      if (name === 'node:path' || name === 'node:crypto') return require(name)
      if (name === 'path') return path
      if (name === 'electron') return electron
      if (name === 'custom-electron-titlebar/main') return { setupTitlebar() {}, attachTitlebarToWindow() {} }
      if (name === 'electron-context-menu') return () => {}
      if (name === './store/config') return { configStore: { get: () => 'https://example.test/im' } }
      if (name === './utils/getEnv') return { getEnv: () => 'production' }
      if (name === './bridge') return { bindBridgeHandler() {} }
      if (name === './bridge/update') return { checkUpdateAndNotify() {} }
      if (name === './menu') return { setMenu() {} }
      if (name === './tray') {
        const TrayClass = load('main/tray/tray.js')
        ipcMain.on('tray:update-menu', () => { if (tray) tray.updateMenus() })
        return { setTray: () => { tray = new TrayClass(); return tray } }
      }
      if (name === './windows' || name === '../windows') return load('main/windows.js')
      throw new Error(`Unexpected dependency ${name} in ${filename}`)
    }
    const source = fs.readFileSync(filename, 'utf8')
    const wrapper = new vm.Script(`(function(require, module, exports, __filename, __dirname) {\n${source}\n})`, { filename })
    wrapper.runInContext(context)(moduleRequire, module, module.exports, filename, path.dirname(filename))
    return module.exports
  }
  load('main/main.js') // Registers the real close lifecycle and before-quit logic.
  for (const callback of ready) callback()
  return { app, created, context, windows: load('main/windows.js'), tray, intervals }
}

const cases = []
function test(name, run) { cases.push([name, run]) }
function restored(win) {
  assert.equal(win.destroyed, false)
  assert.equal(win.visible, true)
  assert.equal(win.skipTaskbar, false)
  assert.equal(win.minimized, false)
}
function windowMenu(env, win) {
  return env.tray.getTray().menu.find((item) => item.label?.endsWith(`Test account ${win.id}`))
}

test('Original reproduces: X hides, tray click and double-click do not restore', () => {
  const env = boot('original')
  const win = env.created[0]
  assert.equal(win.close().defaultPrevented, true)
  assert.equal(win.visible, false)
  assert.equal(win.skipTaskbar, true)
  env.tray.getTray().emit('click')
  env.tray.getTray().emit('double-click')
  assert.equal(win.visible, false)
  assert.equal(win.skipTaskbar, true)
  assert.equal(env.created.length, 1)
})

test('Patched restores the same window and taskbar after two X/restore cycles', () => {
  const env = boot('patched')
  const win = env.created[0]
  for (let round = 0; round < 2; round++) {
    assert.equal(win.close().defaultPrevented, true)
    assert.equal(win.visible, false)
    assert.equal(win.skipTaskbar, true)
    env.tray.getTray().emit('click')
    restored(win)
    assert.equal(win.focused, true)
    assert.equal(env.created.length, 1)
  }
})

test('Patched restores a minimized window', () => {
  const env = boot('patched')
  const win = env.created[0]
  win.minimize()
  env.tray.getTray().emit('click')
  restored(win)
  assert.equal(win.calls.restore, 1)
})

test('Patched restores every account window without creating an account/session', () => {
  const env = boot('patched')
  env.windows.createWindow()
  env.windows.createWindow()
  for (const win of env.created) win.close()
  env.created[1].minimize()
  env.tray.getTray().emit('click')
  for (const win of env.created) restored(win)
  assert.equal(env.created.length, 3)
  assert.equal(env.windows.getAllWindows().length, 3)
})

test('Repeated click/double-click is safe, stops flashing, and creates no instances', () => {
  const env = boot('patched')
  const win = env.created[0]
  win.close()
  env.tray.startFlash()
  assert.equal(env.intervals.size, 1)
  for (const event of ['click', 'click', 'double-click', 'double-click']) env.tray.getTray().emit(event)
  restored(win)
  assert.equal(env.created.length, 1)
  assert.equal(env.tray.timer, undefined)
  assert.equal(env.intervals.size, 0)
})

test('Explicit display submenu restores only the chosen window', () => {
  const env = boot('patched')
  const first = env.created[0]
  const second = env.windows.createWindow()
  first.close()
  second.close()
  const menu = windowMenu(env, first)
  assert.equal(menu.click, undefined)
  menu.submenu.find((item) => item.label === '显示窗口').click()
  restored(first)
  assert.equal(second.visible, false)
  assert.equal(env.created.length, 2)
})

test('Destroyed windows and stale menu callbacks are safe', () => {
  const env = boot('patched')
  const first = env.created[0]
  const second = env.windows.createWindow()
  const staleMenu = windowMenu(env, first)
  const show = staleMenu.submenu.find((item) => item.label === '显示窗口').click
  const destroy = staleMenu.submenu.find((item) => item.label === '退出窗口').click
  destroy()
  assert.equal(first.destroyed, true)
  assert.doesNotThrow(show)
  assert.doesNotThrow(destroy)
  assert.doesNotThrow(() => env.windows.restoreWindow(null))
  assert.doesNotThrow(() => env.windows.restoreWindow(first))
  assert.equal(env.windows.getAllWindows().includes(first), false)
  assert.equal(windowMenu(env, first), undefined)
  second.close()
  assert.doesNotThrow(() => env.tray.getTray().emit('double-click'))
  restored(second)
})

test('A destroyed entry awaiting closed notification is excluded and skipped', () => {
  const env = boot('patched')
  const first = env.created[0]
  const second = env.windows.createWindow()
  first.destroyed = true // Exercise the native-isDestroyed/closed-event boundary.
  assert.equal(env.windows.getAllWindows().includes(first), false)
  assert.doesNotThrow(() => env.tray.updateMenus())
  second.close()
  assert.doesNotThrow(() => env.tray.getTray().emit('click'))
  restored(second)
})

test('Real before-quit handler suppresses restoration and permits close', () => {
  const env = boot('patched')
  const win = env.created[0]
  win.close()
  const staleShow = windowMenu(env, win).submenu.find((item) => item.label === '显示窗口').click
  const shows = win.calls.show
  env.app.emit('before-quit')
  assert.equal(env.context.willQuitApp, true)
  env.tray.getTray().emit('click')
  env.tray.getTray().emit('double-click')
  staleShow()
  env.windows.showAllWindow()
  assert.equal(win.visible, false)
  assert.equal(win.calls.show, shows)
  assert.equal(win.close().defaultPrevented, false)
  assert.equal(win.destroyed, true)
  assert.doesNotThrow(() => env.tray.getTray().emit('click'))
  assert.equal(env.created.length, 1)
})

test('Normal quit closes all windows and tray Exit still exits', () => {
  const env = boot('patched')
  env.windows.createWindow()
  env.app.quit()
  assert.equal(env.context.willQuitApp, true)
  assert.ok(env.created.every((win) => win.destroyed))
  assert.equal(env.windows.getAllWindows().length, 0)
  const exitEnv = boot('patched')
  exitEnv.tray.getTray().menu.find((item) => item.label === '退出小红书私信通').click()
  assert.ok(exitEnv.created.every((win) => win.destroyed))
})

let failed = 0
for (const [name, run] of cases) {
  try { run(); console.log(`PASS ${name}`) }
  catch (error) { failed++; console.error(`FAIL ${name}\n${error.stack}`) }
}
console.log(`\n${cases.length - failed}/${cases.length} regression checks passed`)
process.exitCode = failed ? 1 : 0
