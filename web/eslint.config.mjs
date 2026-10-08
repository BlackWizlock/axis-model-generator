import plugin from '@typescript-eslint/eslint-plugin';
import parser from '@typescript-eslint/parser';
export default [{ignores:['dist/**','node_modules/**','coverage/**']},{
 files:['src/**/*.js','tests/**/*.mjs','*.mjs'],languageOptions:{parser,ecmaVersion:'latest',sourceType:'module'},plugins:{'@typescript-eslint':plugin},
 rules:{eqeqeq:['error','always',{null:'ignore'}],'no-var':'error','prefer-const':'error','prefer-template':'error','no-else-return':['error',{allowElseIf:false}],'no-unsafe-finally':'error','no-param-reassign':['error',{props:false}],'no-throw-literal':'error','no-empty':['error',{allowEmptyCatch:true}],
 'max-lines':['warn',{max:500,skipBlankLines:true,skipComments:true}],'max-lines-per-function':['warn',{max:80,skipBlankLines:true,skipComments:true,IIFEs:true}],'max-params':['warn',4],'max-depth':['warn',4],complexity:['warn',15],'no-await-in-loop':'warn','no-nested-ternary':'warn','no-return-await':'warn','@typescript-eslint/no-shadow':'warn','@typescript-eslint/no-empty-function':'warn','no-warning-comments':['warn',{terms:['todo','fixme','xxx'],location:'anywhere'}]}
},{files:['tests/**/*.mjs'],rules:{'max-lines':'off','max-lines-per-function':'off','max-params':'off',complexity:'off','max-depth':'off','@typescript-eslint/no-empty-function':'off'}}];
