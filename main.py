from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from autogen_core import AgentId, SingleThreadedAgentRuntime
from autogen_core.model_context import BufferedChatCompletionContext
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_ext.tools.mcp import McpWorkbench, SseServerParams
from datetime import datetime
import os
import asyncio
from dotenv import load_dotenv
load_dotenv()

class CompanyRequest(BaseModel):
    tname: str = '中科曙光'
    save_path: str = './'  

app = FastAPI()
import json
from dataclasses import dataclass
from typing import List

from autogen_core import (
    FunctionCall,
    MessageContext,
    RoutedAgent,
    message_handler,
)
from autogen_core.model_context import ChatCompletionContext
from autogen_core.models import (
    AssistantMessage,
    ChatCompletionClient,
    FunctionExecutionResult,
    FunctionExecutionResultMessage,
    LLMMessage,
    SystemMessage,
    UserMessage,
)
from autogen_core.tools import ToolResult, Workbench
@dataclass
class Message:
    content: str


class WorkbenchAgent(RoutedAgent):
    def __init__(
        self, model_client: ChatCompletionClient, model_context: ChatCompletionContext, workbench: Workbench
    ) -> None:
        super().__init__("An agent with a workbench")
        self._system_messages: List[LLMMessage] = [SystemMessage(content="You are a helpful AI assistant.")]
        self._model_client = model_client
        self._model_context = model_context
        self._workbench = workbench

    @message_handler
    async def handle_user_message(self, message: Message, ctx: MessageContext) -> Message:

        await self._model_context.add_message(UserMessage(content=message.content, source="user"))
        print("---------User Message-----------")
        print(message.content)


        create_result = await self._model_client.create(
            messages=self._system_messages + (await self._model_context.get_messages()),
            tools=(await self._workbench.list_tools()),
            cancellation_token=ctx.cancellation_token,
        )


        while isinstance(create_result.content, list) and all(
            isinstance(call, FunctionCall) for call in create_result.content
        ):
            print("---------Function Calls-----------")
            for call in create_result.content:
                print(call)


            await self._model_context.add_message(AssistantMessage(content=create_result.content, source="assistant"))


            print("---------Function Call Results-----------")
            results: List[ToolResult] = []
            B_FLAG =False
            for call in create_result.content:
                result = await self._workbench.call_tool(
                    call.name, arguments=json.loads(call.arguments), cancellation_token=ctx.cancellation_token
                )
                results.append(result)

                print(result)
                if 'browser' in call.name:
                    B_FLAG = True



            await self._model_context.add_message(
                FunctionExecutionResultMessage(
                    content=[
                        FunctionExecutionResult(
                            call_id=call.id,
                            content=result.to_text(),
                            is_error=result.is_error,
                            name=result.name,
                        )
                        for call, result in zip(create_result.content, results, strict=False)
                    ]
                )
            )


            create_result = await self._model_client.create(
                messages=self._system_messages + (await self._model_context.get_messages()),
                tools=(await self._workbench.list_tools()),
                cancellation_token=ctx.cancellation_token,
            )

 
        assert isinstance(create_result.content, str)

        print("---------Final Response-----------")
        print(create_result.content)
        if B_FLAG:
            result = await self._workbench.call_tool(
                'browser_close', arguments=None, cancellation_token=ctx.cancellation_token
            )
            print(result)

        await self._model_context.add_message(AssistantMessage(content=create_result.content, source="assistant"))


        return Message(content=create_result.content)


async def get_info(tname: str, save_path: str):
    research_url = os.getenv("RESEARCH_URL")
    base_url = os.getenv("BASE_URL")
    api_key = os.getenv("API_KEY")
    today = datetime.now().strftime('%Y%m%d')
    playwright_server_params = SseServerParams(url="http://localhost:8931/sse")
    research_server_params = SseServerParams(url=research_url)


    if not os.path.exists(save_path):
        os.makedirs(save_path)

    async with McpWorkbench(playwright_server_params) as workbench:
        runtime = SingleThreadedAgentRuntime()


        await WorkbenchAgent.register(
            runtime=runtime,
            type="WebAgent",
            factory=lambda: WorkbenchAgent(
                model_client=OpenAIChatCompletionClient(
                    model="qwen-plus",
                    base_url=base_url,
                    api_key=api_key,
                    model_info={"function_calling": True, "vision": False, "json_output": True, "family": "unknown"}
                ),
                model_context=BufferedChatCompletionContext(buffer_size=10),
                workbench=workbench,
            ),
        )

        async with McpWorkbench(research_server_params) as workbench2:
            await WorkbenchAgent.register(
                runtime=runtime,
                type="searchAgent",
                factory=lambda: WorkbenchAgent(
                    model_client=OpenAIChatCompletionClient(
                        model="qwen-plus",
                        base_url=base_url,
                        api_key=api_key,
                        model_info={"function_calling": True, "vision": False, "json_output": True, "family": "unknown"}
                    ),
                    model_context=BufferedChatCompletionContext(buffer_size=10),
                    workbench=workbench2,
                ),
            )

            runtime.start()
            task1 = runtime.send_message(
                Message(content=f"使用 Bing 搜索股票{tname}有哪些热点概念"),
                recipient=AgentId("WebAgent", "default"),
            )
            task2 =  runtime.send_message(
                Message(content=f"总结一下{tname}近一个月的研报信息"),
                recipient=AgentId("searchAgent", "default"),
            )
            concept, reasearch = await asyncio.gather(task1, task2)
           
            file_path = os.path.join(save_path, f'{tname}_{today}.md')

      
            with open(file_path, 'w', encoding='utf-8') as fw:
                out_text = '\n\n## 热点概念\n'+concept.content + '\n\n## 研报观点\n' + reasearch.content
                fw.write(out_text)
 
            await runtime.stop()

    return {'message': f'Data successfully fetched and saved to {file_path}.',"text":out_text}

 
@app.post("/get_info")
async def get_company_info(request: CompanyRequest):
    try:
        result = await get_info(tname=request.tname, save_path=request.save_path)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

 
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7000)
