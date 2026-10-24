import { IHistoryProvider, Answers, HistoryProviderOptions, HistoryMetaData } from "./IProvider";

export class NoneProvider implements IHistoryProvider {
    getProviderName = () => HistoryProviderOptions.None;
    resetContinuationToken(): void {
        return;
    }
    async getNextItems(_count: number): Promise<HistoryMetaData[]> {
        return [];
    }
    async addItem(_id: string, _answers: Answers): Promise<void> {
        return;
    }
    async getItem(_id: string): Promise<null> {
        return null;
    }
    async deleteItem(_id: string): Promise<void> {
        return;
    }
}
