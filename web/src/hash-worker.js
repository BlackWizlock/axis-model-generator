import {hashSlices} from './sha256.js';
self.onmessage = async ({data}) => {
  try {const digest=await hashSlices(data.file,(done,total)=>self.postMessage({type:'progress',done,total}));self.postMessage({type:'done',digest});}
  catch {self.postMessage({type:'error'});}
  finally {self.close();}
};
