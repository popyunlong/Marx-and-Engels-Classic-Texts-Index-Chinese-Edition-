(function (host) {
  "use strict";
  var labels = {approximate_quote:"近似引文", elided_quote:"节引", pending_quote:"待核验"};
  function norm(s) { return String(s || "").normalize("NFKC").replace(/[^\p{L}\p{N}]/gu, ""); }
  function distance(a,b,limit) {
    var prev = Array.from({length:b.length+1},function(_,i){return i;});
    for(var i=1;i<=a.length;i++) {
      var row=Array(b.length+1).fill(limit+1); row[0]=i;
      for(var j=Math.max(1,i-limit);j<=Math.min(b.length,i+limit);j++) row[j]=Math.min(prev[j]+1,row[j-1]+1,prev[j-1]+(a[i-1]===b[j-1]?0:1));
      prev=row;
    }
    return prev[b.length];
  }
  var cache = new WeakMap();
  function project(message) {
    if(!Array.isArray(message.citations)) return message;
    var memo=cache.get(message.citations);
    if(memo && memo.content===message.content) return Object.assign({},message,{citations:memo.citations});
    var cards=message.citations.map(function(c){return Object.assign({},c,{evidence:(c.evidence||[]).slice()});});
    var until=Date.now()+80, fenced=false;
    String(message.content||"").split(/\n/).forEach(function(line){
      if(/^\s*(```|~~~)/.test(line)){fenced=!fenced;return;}
      if(fenced || /^\s*#/.test(line) || /`|!?\[[^\]]+\]\(/.test(line))return;
      var refs=(line.match(/\[\d+\]/g)||[]).map(function(x){return Number(x.slice(1,-1));});
      var candidates=Array.from(line.matchAll(/[“「『]([^”」』]+)[”」』]/g)).map(function(m){return {text:m[1],explicit:true};});
      if(!candidates.length)candidates=[{text:line.replace(/^\s*>\s*/,'').replace(/\[\d+\]/g,'').trim(),explicit:/^\s*>/.test(line)}];
      candidates.forEach(function(candidate){
        var text=candidate.text,a=norm(text), hits=[];
        if(a.length<(candidate.explicit?12:20)||a.length>600||Date.now()>until)return;
        if(cards.some(function(c){return c.evidence.some(function(e){return e.answer_text===text;});}))return;
        var pool=cards.filter(function(c){return !refs.length||refs.indexOf(Number(c.grounding_index||c.review_index))>=0;});
        pool.forEach(function(c){
          var sources=c.evidence.map(function(e){return e.source_text||e.quote||e.context;}).concat(c.context||[]);
          sources.forEach(function(raw){
            var source=String(raw||'').replace(/\[\[\/?H\]\]/g,''), b=norm(source), limit=Math.min(6,Math.floor(a.length/10));
            if(!b||b.indexOf(a)>=0||Date.now()>until)return;
            var parts=text.split(/…+|\.{3,}|〔略〕|\[略\]/).map(norm), end=0, first=-1;
            if(parts.length>1&&parts.every(function(x){return x.length>=4;})){
              if(parts.every(function(x){var at=b.indexOf(x,end);if(at<0)return false;if(first<0)first=at;end=at+x.length;return true;}))hits.push({card:c,kind:'elided_quote',source:source,score:0});
              return;
            }
            var starts=new Set();
            for(var seed=0;seed<a.length-5;seed+=6){var at=b.indexOf(a.slice(seed,seed+6));while(at>=0&&starts.size<32){starts.add(at-seed);at=b.indexOf(a.slice(seed,seed+6),at+1);}}
            starts.forEach(function(start){
              for(var left=Math.max(0,start-limit);left<=Math.min(b.length,start+limit)&&Date.now()<=until;left++){
                for(var len=a.length-limit;len<=a.length+limit;len++){
                  if(left+len>b.length)continue;
                  var literal=b.slice(left,left+len), score=distance(a,literal,limit);
                  if(score<=limit){
                    var critical=function(x){return (x.match(/[不未无非没勿莫否\d一二三四五六七八九十百千万亿]/g)||[]).join('');};
                    hits.push({card:c,source:source,span:literal,score:score,kind:critical(a)===critical(literal)?'approximate_quote':'pending_quote'});
                  }
                }
              }
            });
          });
        });
        hits.sort(function(a,b){return a.score-b.score;});
        if(!hits.length)return;
        var best=hits[0], tied=hits.filter(function(h){return h.score===best.score;});
        var identities=new Set(tied.map(function(h){return (h.card.source_file||h.card.document_id||h.card.grounding_index||h.card.review_index)+'|'+(h.span||h.source);}));
        var kind=identities.size>1||Date.now()>until?'pending_quote':best.kind;
        best.card.evidence.push({kind:kind,answer_text:text,quote:text,source_text:best.source,text_verified:false,
          location_status:'unresolved',verification_reason:kind==='pending_quote'?'历史证据存在关键差异或来源歧义':'依据已保存证据显示，页码未重新核验'});
      });
    });
    cache.set(message.citations,{content:message.content,citations:cards});
    return Object.assign({},message,{citations:cards});
  }
  function mark(content,cards){
    var result=content;
    (cards||[]).forEach(function(c){(c.evidence||[]).forEach(function(e){
      if(!labels[e.kind]||!e.answer_text)return;
      var protectedRanges=[];
      result.replace(/^```[^\n]*\n[\s\S]*?^```[^\n]*$|^~~~[^\n]*\n[\s\S]*?^~~~[^\n]*$|`[^`\n]*`|!?\[[^\]\n]*\]\([^\n)]*\)|^#{1,6}[^\n]*$/gm,function(m,at){protectedRanges.push([at,at+m.length]);return m;});
      var visible='',offsets=[];
      Array.from(result).reduce(function(offset,ch){if(!/[\s*_]/.test(ch)){visible+=ch;for(var j=0;j<ch.length;j++)offsets.push(offset);}return offset+ch.length;},0);
      var needle=e.answer_text.replace(/[\s*_]/g,''), edits=[], at=visible.indexOf(needle);
      if(!needle)return;
      while(at>=0){
        var start=offsets[at],end=offsets[at+needle.length-1]+1;
        while(/[*_]/.test(result[end]||' ')&&end<result.length)end++;
        if(/[”」』]/.test(result[end]||''))end++;
        if(protectedRanges.some(function(r){return start<r[1]&&end>r[0];})){at=visible.indexOf(needle,at+needle.length);continue;}
        if(result.slice(end,end+1)!=='〔') {
          var id=Number(c.grounding_index||c.review_index), tail=result.slice(end).match(/^\s*((?:\[\d+(?:[,，、]\d+)*\])*)/);
          var cited=tail && (tail[1].match(/\d+/g)||[]).map(Number).indexOf(id)>=0;
          var marker=e.kind!=='pending_quote' && id && !cited ? '['+id+']' : '';
          edits.push({end:end,text:'〔'+labels[e.kind]+'〕'+marker});
        }
        at=visible.indexOf(needle,at+needle.length);
      }
      edits.reverse().forEach(function(edit){result=result.slice(0,edit.end)+edit.text+result.slice(edit.end);});
    });});return result;
  }
  host.AipQuoteVariants={project:project,mark:mark,labels:labels};
})(typeof window==='undefined'?globalThis:window);
