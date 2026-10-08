import {useCallback,useEffect,useRef,useState} from 'react';
import {api} from './api';
export function useResource<T>(path:string|null,tick=0,loader?:()=>Promise<T>){
  const [data,setData]=useState<T|null>(null),[error,setError]=useState<string|null>(null),[loading,setLoading]=useState(false);
  const current=useRef(path),sequence=useRef(0);current.current=path;
  const reload=useCallback(async()=>{if(!path)return;const id=++sequence.current;setLoading(true);try{const result=await (loader?loader():api.get<T>(path));if(id===sequence.current&&current.current===path){setData(result);setError(null);}}catch(err){if(id===sequence.current&&current.current===path)setError(err instanceof Error?err.message:'Не удалось загрузить данные');}finally{if(id===sequence.current)setLoading(false);}},[path,loader]);
  useEffect(()=>{setData(null);setError(null);},[path]);
  useEffect(()=>{void reload();return()=>{sequence.current++;};},[reload,tick]);
  return {data,error,loading,reload};
}
