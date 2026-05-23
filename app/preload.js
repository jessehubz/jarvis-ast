const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electron', {
  minimize:     () => ipcRenderer.send('win-minimize'),
  close:        () => ipcRenderer.send('win-close'),
  resizeIsland: (expanded) => ipcRenderer.send('island-resize', expanded),
})
