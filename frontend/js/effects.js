// Озвучка и телефонное аудио выполняются последовательно, отдельно от применения событий UI.
export class EffectQueue {
  constructor(limit = 128, onOverflow = () => {}) {
    this.limit = limit; this.onOverflow = onOverflow;
    this.jobs = []; this.running = false; this.epoch = 0;
  }
  reset() { this.epoch++; this.jobs = []; this.running = false; }
  enqueue(job) {
    if (this.jobs.length >= this.limit) { this.reset(); this.onOverflow(); return; }
    this.jobs.push(job);
    if (!this.running) { this.running = true; this.pump(this.epoch); }
  }
  async pump(epoch) {
    while (epoch === this.epoch && this.jobs.length) {
      const job = this.jobs.shift();
      try { await job(); } catch (error) { console.error(error); }
    }
    if (epoch === this.epoch) this.running = false;
  }
}
