/* Continuous area-average resampling; no per-block rounding drift at 44.1/48 kHz. */
class PCMRecorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.remaining = this.ratio;
    this.total = 0;
    this.samples = [];
    this.active = true;
    this.port.onmessage = ({data}) => {
      if (data === "flush") {
        this.active = false;
        // Complete the last partial resampling interval without dropping audio.
        if (this.remaining < this.ratio) {
          this.samples.push(this.total / (this.ratio - this.remaining));
          this.total = 0;
          this.remaining = this.ratio;
        }
        this.emit();
        this.port.postMessage({type: "flushed"});
      }
    };
  }
  emit() {
    if (!this.samples.length) return;
    const buffer = new ArrayBuffer(this.samples.length * 2);
    const view = new DataView(buffer);
    let peak = 0;
    this.samples.forEach((value, index) => {
      value = Math.max(-1, Math.min(1, value));
      const sample = Math.round(value * (value < 0 ? 32768 : 32767));
      peak = Math.max(peak, Math.abs(sample));
      view.setInt16(index * 2, sample, true);
    });
    this.samples = [];
    this.port.postMessage({type: "pcm", buffer, peak}, [buffer]);
  }
  process(inputs) {
    const channels = inputs[0];
    if (!this.active || !channels?.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let value = 0;
      for (const channel of channels) value += channel[i] / channels.length;
      let available = 1;
      while (available > 1e-8) {
        const weight = Math.min(available, this.remaining);
        this.total += value * weight;
        this.remaining -= weight;
        available -= weight;
        if (this.remaining < 1e-8) {
          this.samples.push(this.total / this.ratio);
          this.remaining = this.ratio;
          this.total = 0;
          if (this.samples.length >= 1600) this.emit();
        }
      }
    }
    return true;
  }
}
registerProcessor("pcm-recorder", PCMRecorder);