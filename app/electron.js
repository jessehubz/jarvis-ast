const { app, BrowserWindow, ipcMain, screen } = require('electron')
const path = require('path')

const isDev = process.env.NODE_ENV !== 'production'

let mainWindow   = null
let islandWindow = null

function createMain() {
  mainWindow = new BrowserWindow({
    width: 900,
    height: 620,
    minWidth: 720,
    minHeight: 500,
    frame: false,
    transparent: false,
    backgroundColor: '#0a0a0a',
    titleBarStyle: 'hidden',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })
  isDev
    ? mainWindow.loadURL('http://localhost:5173')
    : mainWindow.loadFile(path.join(__dirname, 'dist/index.html'))
}

function createIsland() {
  const { width } = screen.getPrimaryDisplay().workAreaSize
  islandWindow = new BrowserWindow({
    width: 260,
    height: 46,
    x: Math.round((width - 260) / 2),
    y: 16,
    transparent: true,
    frame: false,
    alwaysOnTop: true,
    focusable: false,
    hasShadow: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })
  isDev
    ? islandWindow.loadURL('http://localhost:5173/?view=island')
    : islandWindow.loadFile(path.join(__dirname, 'dist/index.html'), { query: { view: 'island' } })
}

ipcMain.on('win-minimize', () => mainWindow?.minimize())
ipcMain.on('win-close', () => {
  islandWindow?.close()
  app.quit()
})
ipcMain.on('island-resize', (_, expanded) => {
  if (!islandWindow) return
  const { width } = screen.getPrimaryDisplay().workAreaSize
  const [W, H] = expanded ? [420, 100] : [260, 46]
  islandWindow.setBounds({ x: Math.round((width - W) / 2), y: 16, width: W, height: H }, true)
})

app.whenReady().then(() => {
  createMain()
  createIsland()
})

app.on('window-all-closed', () => app.quit())
