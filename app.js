const POLL_INTERVAL_MS = 500;
const pinState = document.querySelector('#pin-state');
const statusDetail = document.querySelector('#status-detail');
const connectionText = document.querySelector('#connection-text');
const lastUpdated = document.querySelector('#last-updated');
let consecutiveErrors = 0;

function showError(message) {
  document.body.className = 'state-error';
  pinState.textContent = 'UNAVAILABLE';
  statusDetail.textContent = message;
  connectionText.textContent = 'Connection issue';
  lastUpdated.textContent = 'Retrying automatically';
}

async function readPin() {
  try {
    const response = await fetch('/api/status', { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok || !data.ok) {
      throw new Error(data.error || `Server returned ${response.status}`);
    }

    consecutiveErrors = 0;
    document.body.className = data.high ? 'state-high' : 'state-low';
    pinState.textContent = data.high ? 'GPIO HIGH' : 'GPIO LOW';
    statusDetail.textContent = data.high
      ? 'Input is idle · pull-up voltage detected'
      : 'Input is active · pin is pulled to ground';
    connectionText.textContent = 'Live';
    lastUpdated.textContent = `Updated ${new Date(data.updated_at).toLocaleTimeString()}`;
  } catch (error) {
    consecutiveErrors += 1;
    showError(consecutiveErrors > 1 ? 'Cannot reach the GPIO monitor. Retrying…' : error.message);
  }
}

readPin();
window.setInterval(readPin, POLL_INTERVAL_MS);
